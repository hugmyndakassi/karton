"""
Base library for karton subsystems.
"""
import abc
import argparse
import contextlib
import json
import textwrap
import time

from .base import KartonBase, KartonServiceBase
from .config import Config
from .resource import LocalResource
from .task import Task, TaskState, TaskPriority
from .utils import GracefulKiller, get_function_arg_num

from .__version__ import __version__


TASKS_QUEUE = "karton.tasks"
TASK_PREFIX = "karton.task:"
METRICS_PRODUCED = "karton.metrics.produced"
METRICS_CONSUMED = "karton.metrics.consumed"
METRICS_ERRORED = "karton.metrics.errored"


class Producer(KartonBase):
    """
    Producer class. Used for dispatching initial tasks into karton.

    :param config: Karton configuration object (optional)
    :type config: :class:`karton.Config`
    :param identity: Producer name (optional)
    :type identity: str

    Usage example:

    .. code-block:: python

        from karton.core import Producer

        producer = Producer(identity="karton.mwdb")
        task = Task(
            headers={
                "type": "sample",
                "kind": "raw"
            },
            payload={
                "sample": Resource("sample.exe", b"put content here")
            }
        )
        producer.send_task(task)
    """

    def __init__(self, config=None, identity=None):
        super(Producer, self).__init__(config=config, identity=identity)

    def send_task(self, task):
        """
        Sends a task to the unrouted task queue. Takes care of logging.
        Given task will be child of task we are currently handling (if such exists).

        :param task: task to be sent
        :type task: :py:class:`karton.Task`
        :rtype: bool
        :return: if task was delivered
        """
        self.log.debug("Dispatched task %s", task.uid)

        # Complete information about task
        if self.current_task is not None:
            task.set_task_parent(self.current_task)
            task.merge_persistent_payload(self.current_task)
            task.priority = self.current_task.priority

        task.last_update = time.time()
        task.headers.update({"origin": self.identity})

        # Ensure all local resources have good buckets
        for _, resource in task.iterate_resources():
            if isinstance(resource, LocalResource) and not resource.bucket:
                resource.bucket = self.config.minio_config["bucket"]

        task_json = task.serialize()

        # Declare task
        self.rs.set(TASK_PREFIX + task.uid, task_json)

        # Upload local resources
        for _, resource in task.iterate_resources():
            if isinstance(resource, LocalResource):
                resource.upload(self.minio)

        # Add task to TASKS_QUEUE
        self.rs.rpush(TASKS_QUEUE, task.uid)
        self.rs.hincrby(METRICS_PRODUCED, self.identity, 1)
        return True

    @contextlib.contextmanager
    def continue_asynchronic(self, task, finish=True):
        """
        Continue asynchronic task. This is wrapper code used for resuming asynchronic task, takes care of setting
        context and logging when the task is finished. That is when the context manager finishes.

        .. note::

            This is experimental feature.
            Read about asynchronic tasks in Advanced concepts.

        :param task: task to be resumed
        :type task: :py:class:`karton.Task`
        :param finish: if we should log that the task finished
        :type finish: bool
        """

        old_current_task = self.current_task

        self.current_task = task
        self.log_handler.set_task(self.current_task)

        # Handle task
        yield

        # Finish task
        if finish:
            self.declare_task_state(
                self.current_task, status=TaskState.FINISHED, identity=self.identity,
            )

        self.current_task = old_current_task
        self.log_handler.set_task(self.current_task)


class Consumer(KartonServiceBase):
    """
    Base consumer class
    """

    filters = None
    persistent = True
    version = None

    def __init__(self, config=None, identity=None):
        super(Consumer, self).__init__(config=config, identity=identity)

        self.current_task = None
        self.shutdown = False
        self.killer = GracefulKiller(self.graceful_shutdown)

        self._pre_hooks = []
        self._post_hooks = []

    def graceful_shutdown(self):
        self.log.info("Gracefully shutting down!")
        self.shutdown = True

    @abc.abstractmethod
    def process(self, *args):
        """
        Task processing method.

        self.current_task contains task that triggered invocation of :py:meth:`karton.Consumer.process`
        """
        raise NotImplementedError()

    def internal_process(self, data):
        self.current_task = Task.unserialize(self.rs.get("karton.task:" + data), minio=self.minio)
        self.log_handler.set_task(self.current_task)

        for bind in self.filters:
            if self.current_task.matches_bind(bind):
                break
        else:
            self.log.info("Task rejected because binds are no longer valid.")
            self.declare_task_state(
                self.current_task, TaskState.FINISHED, identity=self.identity,
            )
            return

        try:
            self.log.info("Received new task - %s", self.current_task.uid)
            self.declare_task_state(
                self.current_task, TaskState.STARTED, identity=self.identity
            )

            self._run_pre_hooks()

            # check if the process function expects the current task or not
            try:
                if get_function_arg_num(self.process) == 0:
                    self.process()
                else:
                    self.process(self.current_task)
                saved_exception = None
            except Exception as exc:
                saved_exception = exc
                raise
            finally:
                self._run_post_hooks(saved_exception)

            self.log.info("Task done - %s", self.current_task.uid)
        except Exception:
            self.rs.hincrby(METRICS_ERRORED, self.identity, 1)
            self.log.exception(
                "Failed to process task - %s", self.current_task.uid
            )
        finally:
            self.rs.hincrby(METRICS_CONSUMED, self.identity, 1)
            if not self.current_task.is_asynchronic():
                self.declare_task_state(
                    self.current_task, TaskState.FINISHED, identity=self.identity,
                )

    @property
    def _registration(self):
        return json.dumps({
            "info": self.__class__.__doc__,
            "version": __version__,
            "filters": self.filters,
            "persistent": self.persistent
        }, sort_keys=True)

    def add_pre_hook(self, callback, name=None):
        """
        Add a function to be called before processing each task.

        :param callback: Function of the form ``callback(task)`` where ``task``
            is a :class:`karton.Task`
        :type callback: function
        :param name: Name of the pre-hook
        :type name: str, optional
        """
        self._pre_hooks.append((name, callback))

    def add_post_hook(self, callback, name=None):
        """
        Add a function to be called after processing each task.

        :param callback: Function of the form ``callback(task, exception)``
            where ``task`` is a :class:`karton.Task` and ``exception`` is
            an exception thrown by the :meth:`karton.Consumer.process` function
            or ``None``.
        :type callback: function
        :param name: Name of the post-hook
        :type name: str, optional
        """
        self._post_hooks.append((name, callback))

    def _run_pre_hooks(self):
        """ Run registered preprocessing hooks """
        for name, callback in self._pre_hooks:
            try:
                callback(self.current_task)
            except Exception:
                if name:
                    self.log.exception("Pre-hook (%s) failed", name)
                else:
                    self.log.exception("Pre-hook failed")

    def _run_post_hooks(self, exception):
        """ Run registered postprocessing hooks """
        for name, callback in self._post_hooks:
            try:
                callback(self.current_task, exception)
            except Exception:
                if name:
                    self.log.exception("Post-hook (%s) failed", name)
                else:
                    self.log.exception("Post-hook failed")

    def loop(self):
        """
        Blocking loop that consumes tasks and runs :py:meth:`karton.Consumer.process` as a handler
        """
        self.log.info("Service %s started", self.identity)

        # get the old binds and set the new ones atomically
        with self.rs.pipeline(transaction=True) as pipe:
            pipe.hget("karton.binds", self.identity)
            pipe.hset("karton.binds", self.identity, self._registration)
            old_registration, _ = pipe.execute()

        if not old_registration:
            self.log.info("Service binds created.")
        elif old_registration != self._registration:
            self.log.info("Binds changed, old service instances should exit soon.")

        for task_filter in self.filters:
            self.log.info("Binding on: %s", task_filter)

        self.rs.client_setname(self.identity)

        try:
            while not self.shutdown:
                if self.rs.hget("karton.binds", self.identity) != self._registration:
                    self.log.info("Binds changed, shutting down.")
                    break

                item = self.rs.blpop(
                    [
                        self.identity,  # Backwards compatibility, remove after upgrade
                        "karton.queue.{}:{}".format(TaskPriority.HIGH, self.identity),
                        "karton.queue.{}:{}".format(TaskPriority.NORMAL, self.identity),
                        "karton.queue.{}:{}".format(TaskPriority.LOW, self.identity),
                    ],
                    timeout=5,
                )

                if item:
                    queue, data = item
                    self.internal_process(data)
        except KeyboardInterrupt as e:
            self.log.info("Hard shutting down!")
            raise e


class LogConsumer(KartonServiceBase):
    """
    Base class for log consumer subsystems
    """
    def __init__(self, config=None, identity=None):
        super(LogConsumer, self).__init__(config=config, identity=identity)
        self.shutdown = False
        self.killer = GracefulKiller(self.graceful_shutdown)

    def graceful_shutdown(self):
        self.log.info("Gracefully shutting down!")
        self.shutdown = True

    @abc.abstractmethod
    def process_log(self, event):
        """
        Expected to be overloaded
        """
        raise NotImplementedError()

    def loop(self):
        self.log.info("Logger %s started", self.identity)

        while not self.shutdown:
            data = self.rs.blpop("karton.logs")
            if data:
                queue, body = data
                try:
                    body = json.loads(body)
                    if "task" in body and isinstance(body["task"], str):
                        body["task"] = json.loads(body["task"])
                    self.process_log(body)
                except Exception:
                    """
                    This is log handler exception, so DO NOT USE self.log HERE!
                    """
                    import traceback
                    traceback.print_exc()


class Karton(Consumer, Producer):
    """
    This glues together Consumer and Producer - which is the most common use case
    """