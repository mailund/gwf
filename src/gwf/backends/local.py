import json
import logging
import multiprocessing
import os
import os.path
import socket
import socketserver
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from enum import Enum

from ..conf import config
from ..utils import PersistableDict
from . import Backend, Status
from .exceptions import BackendError, DependencyError
from .logmanager import FileLogManager

__all__ = ("Client", "Server", "LocalBackend", "start_server")

logger = logging.getLogger(__name__)


def _gen_task_id():
    return uuid.uuid4().hex


class LocalStatus(Enum):
    UNKNOWN = 0
    SUBMITTED = 1
    RUNNING = 2
    FAILED = 3
    COMPLETED = 4
    CANCELLED = 5


class LocalBackend(Backend):
    """Backend that runs targets on a local cluster.

    To use this backend you must activate the `local` backend and start a
    local cluster (with one or more workers) that the backend can submit targets
    to. To start a cluster with two workers run the command::

        gwf -b local workers -n 2

    in the working directory of your project. The workflow file must be accessible
    to *gwf*. Thus, if your workflow file is not called `workflow.py` or the
    workflow object is not called `gwf`, you must specify this so that *gwf* can
    locate the workflow::

        gwf -f myworkflow.py:wf1 -b local workers -n 2

    If the local backend is your default backend you can of course omit the
    ``-b local`` option.

    If the ``-n`` option is omitted, *gwf* will detect the number of cores
    available and use all of them.

    To run your workflow, open another terminal and then type::

        gwf -b local run

    To stop the pool of workers press :kbd:`Control-c`.

    **Backend options:**

    * **local.host (str):** Set the host that the workers are running on (default: localhost).
    * **local.port (int):** Set the port used to connect to the workers (default: 12345).

    **Target options:**

    None available.
    """

    log_manager = FileLogManager()

    option_defaults = {"cores": 1}

    def __init__(self):
        super().__init__()

        self._tracked = PersistableDict(os.path.join(".gwf/local-backend-tracked.json"))

        host = config.get("local.host", "localhost")
        port = config.get("local.port", 12345)
        try:
            self.client = Client(host, port)
            self.client.connect()
        except ConnectionRefusedError:
            raise BackendError(
                "Local backend could not connect to workers on port {}. "
                'Workers can be started by running "gwf workers". '
                "You can read more in the documentation: "
                "https://gwf.app/reference/backends/#gwf.backends.local.LocalBackend".format(
                    port
                )
            )

    def get_task_id(self, target_name):
        return self._tracked[target_name]

    def submit(self, target, dependencies):
        try:
            dependency_ids = [self._tracked[dep.name] for dep in dependencies]
        except KeyError as exc:
            (key,) = exc.args
            raise DependencyError(key)

        env = dict(os.environ)
        env["GWF_TARGET_NAME"] = target.name

        resources = dict(self.option_defaults)
        resources.update(target.options)

        task_id = self.client.submit(
            script=target.spec,
            working_dir=target.working_dir,
            env=env,
            resources=resources,
            dependencies=dependency_ids,
        )
        self._tracked[target.name] = task_id

    def cancel(self, target):
        self.client.cancel(self._tracked[target.name])

    def status(self, target):
        if target.name not in self._tracked:
            return Status.UNKNOWN

        task_id = self._tracked[target.name]
        status = self.client.status(task_id)
        if status == LocalStatus.RUNNING:
            return Status.RUNNING
        elif status == LocalStatus.SUBMITTED:
            return Status.SUBMITTED
        return Status.UNKNOWN

    def logs(self, target, stderr=False):
        """Return log files for a target.

        If the backend cannot return logs a
        :class:`~gwf.exceptions.NoLogFoundError` is raised.

        By default standard output (stdout) is returned. If `stderr=True`
        standard error will be returned instead.

        :param gwf.Target target:
            Target to return logs for.
        :param bool stderr:
            default: False. If true, return standard error.
        :return:
            A file-like object. The user is responsible for closing the
            returned file(s) after use.
        :raises gwf.exceptions.NoLogFoundError:
            if the backend could not find a log for the given target.
        """
        if stderr:
            task_id = self.get_task_id(target.name)
            return self.log_manager.open_stderr(task_id)
        return self.log_manager.open_stdout(task_id)

    def close(self):
        self._tracked.persist()
        self.client.close()


class Task:
    def __init__(
        self,
        id,
        script="",
        working_dir=None,
        env=None,
        resources=None,
        dependencies=None,
    ):
        self.id = id
        self.script = script
        self.working_dir = working_dir
        self.env = env or {}
        self.resources = resources or {}

        if dependencies is None:
            self.dependencies = set()
        else:
            self.dependencies = set(dependencies)

    def __repr__(self):
        return "Task(id={!r})".format(self.id)

    def __str__(self):
        self.id


class ExecutorError(Exception):
    pass


class Executor:
    def __init__(self, scheduler, log_manager, kill_timeout=10):
        self._scheduler = scheduler
        self._kill_timeout = kill_timeout
        self._log_manager = log_manager

        self._terminated = threading.Event()
        self._cancelled = threading.Event()

        self._thread = None
        self._task = None

    def terminate(self):
        self._terminated.set()

    def cancel(self):
        self._cancelled.set()

    def wait(self):
        self._thread.join()

    def execute(self, task):
        self._task = task

        thread_name = "local-executor-{}".format(self._task.id)
        self._thread = threading.Thread(target=self._execute, name=thread_name)
        self._thread.start()

    def update_status(self, status):
        self._scheduler.set_status(self._task.id, status)

    def terminate_gracefully(self, process, poll_interval=1):
        process.terminate()

        timed_out = True
        for time_spent in range(self._kill_timeout):
            if process.poll() is not None:
                timed_out = False
                break
            time.sleep(poll_interval)

        if timed_out:
            process.kill()

    def _execute(self):
        self.update_status(LocalStatus.RUNNING)

        stdout_file = self._log_manager.open_stdout(self._task.id, mode="w")
        stderr_file = self._log_manager.open_stderr(self._task.id, mode="w")
        try:
            process = subprocess.Popen(
                ["/bin/bash"],
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                universal_newlines=True,
                cwd=self._task.working_dir,
                env=self._task.env,
            )

            process.stdin.write(self._task.script)
            process.stdin.flush()
            process.stdin.close()

            while (
                process.poll() is None
                and not self._terminated.is_set()
                and not self._cancelled.is_set()
            ):
                time.sleep(0.1)

        except Exception:
            logger.error(
                "Executor %s failed unexpectedly", self._task.id, exc_info=True
            )
            self.update_status(LocalStatus.FAILED)
        else:
            if self._terminated.is_set():
                self.terminate_gracefully(process)
                self.update_status(LocalStatus.FAILED)
            elif self._cancelled.is_set():
                self.terminate_gracefully(process)
                self.update_status(LocalStatus.CANCELLED)
            elif process.returncode == 0:
                self.update_status(LocalStatus.COMPLETED)
            else:
                logger.debug(
                    "Task %s failed with exit code %s",
                    self._task.id,
                    process.returncode,
                )
                self.update_status(LocalStatus.FAILED)
        finally:
            stdout_file.close()
            stderr_file.close()
            logger.debug("Exiting executor")


class TaskScheduler:

    FAILED_STATES = (LocalStatus.CANCELLED, LocalStatus.FAILED)
    FINISHED_STATES = (LocalStatus.CANCELLED, LocalStatus.FAILED, LocalStatus.COMPLETED)

    def __init__(self, max_cores, log_manager):
        self._max_cores = max_cores
        self._log_manager = log_manager

        self._queue = OrderedDict()
        self._dependents = defaultdict(set)
        self._status_map = {}
        self._executors = {}
        self._available_cores = max_cores
        self._lock = threading.Lock()

        self._shutdown = False
        self._thread = None

    def enqueue_task(self, task):
        with self._lock:
            self._queue[task.id] = task
            self._set_status(task.id, LocalStatus.SUBMITTED)

    def cancel_task(self, task_id):
        with self._lock:
            self._set_status(task_id, LocalStatus.CANCELLED)

    def get_status(self, task_id):
        return self._status_map.get(task_id, LocalStatus.UNKNOWN)

    def set_status(self, task_id, status):
        with self._lock:
            self._set_status(task_id, status)

    def schedule_once(self):
        scheduled = []
        failed = []
        with self._lock:
            available_cores = self._available_cores
            for task in self._queue.values():
                if available_cores == 0:
                    break

                has_failed_dep = any(
                    self._status_map[dep_id] in self.FAILED_STATES
                    for dep_id in task.dependencies
                )

                if has_failed_dep:
                    failed.append(task)
                    continue

                can_run = all(
                    self._status_map[dep_id] == LocalStatus.COMPLETED
                    for dep_id in task.dependencies
                )

                if can_run:
                    available_cores -= task.resources["cores"]
                    scheduled.append(task)

            for task in scheduled:
                self._set_status(task.id, LocalStatus.RUNNING)

    def schedule_forever(self):
        logger.debug("Starting scheduling")
        while not self._shutdown:
            self.schedule_once()
            time.sleep(0.1)

    def shutdown(self):
        self._shutdown = True

    def wait(self):
        while True:
            with self._lock:
                if not self._executors:
                    break
            time.sleep(0.1)
        if self._thread is not None:
            self._thread.join()

    def _set_status(self, task_id, status):
        old_status = self._status_map.get(task_id, LocalStatus.UNKNOWN)
        if old_status == status:
            return

        if status == LocalStatus.SUBMITTED and old_status == LocalStatus.UNKNOWN:
            logger.debug("Task %s submitted", task_id)
            task = self._queue[task_id]
            for dep_id in task.dependencies:
                if self._status_map.get(dep_id) is None:
                    raise BackendError("Unknown dependency '{}'".format(dep_id))

                if self._status_map[dep_id] in self.FAILED_STATES:
                    self._set_status(task_id, LocalStatus.FAILED)
                    return

                self._dependents[dep_id].add(task.id)
        elif status == LocalStatus.RUNNING and old_status == LocalStatus.SUBMITTED:
            logger.debug("Task %s started", task_id)
            self._available_cores -= 1
            executor = Executor(scheduler=self, log_manager=self._log_manager)
            self._executors[task_id] = executor
            executor.execute(self._queue[task_id])
            del self._queue[task_id]
        elif status == LocalStatus.CANCELLED and old_status == LocalStatus.RUNNING:
            logger.debug("Task %s cancelled", task_id)
            executor = self._executors[task_id]
            executor.cancel()
            del self._executors[task_id]
        elif status == LocalStatus.CANCELLED and old_status == LocalStatus.SUBMITTED:
            logger.debug("Task %s cancelled", task_id)
            del self._queue[task_id]
        elif status in self.FINISHED_STATES:
            logger.debug("Task %s finished", task_id)
            self._available_cores += 1
            if old_status == LocalStatus.RUNNING:
                del self._executors[task_id]

        self._status_map[task_id] = status
        if status in self.FAILED_STATES:
            for dep_id in self._dependents[task_id]:
                self._set_status(dep_id, LocalStatus.FAILED)


class ConnectionHandler(socketserver.BaseRequestHandler):
    def handle(self):
        logger.debug("Accepted connection from %s", self.client_address[0])
        scheduler = self.server.scheduler
        while True:
            message = self.receive_message()
            if message is None:
                break

            request_type = message.pop("type")
            if request_type == "submit-task":
                scheduler.enqueue_task(Task(**message))
                self.send_message({"type": "ok"})
            elif request_type == "cancel-task":
                scheduler.cancel_task(message["id"])
                self.send_message({"type": "ok"})
            elif request_type == "get-status":
                status = scheduler.get_status(message["id"])
                self.send_message({"type": "status", "status": status.name})

    def receive_message(self):
        data = self.request.recv(8192)
        if not data:
            return None
        message = json.loads(data.decode("utf-8"))
        return message

    def send_message(self, message):
        data = json.dumps(message)
        self.request.sendall(bytes(data + "\n", "utf-8"))


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


class Server:
    def __init__(self, scheduler, host, port):
        self._scheduler = scheduler
        self._host = host
        self._port = port

        self._tcp_server = _ThreadedTCPServer(
            (self._host, self._port), ConnectionHandler
        )
        self._tcp_server.scheduler = scheduler

        self._tcp_server_thread = None
        self._scheduler_thread = None

    def get_scheduler(self):
        return self._scheduler

    def start(self):
        self._scheduler_thread = threading.Thread(
            target=self._scheduler.schedule_forever, name="local-scheduler"
        )
        self._scheduler_thread.start()

        self._tcp_server_thread = threading.Thread(
            target=self._tcp_server.serve_forever, name="local-server"
        )
        self._tcp_server_thread.start()

        logger.info("Server listening on %s port %d", self._host, self._port)

    def shutdown(self):
        self._tcp_server.shutdown()
        self._tcp_server.server_close()
        self._tcp_server_thread.join()

        self._scheduler.shutdown()
        self._scheduler_thread.join()


def start_server(log_manager, host="127.0.0.1", port=12345, max_cores=None):
    """Start a local backend server.

    Accepts a `host` and `port` to connect to. The `host` defaults to 127.0.0.1
    (localhost) and the port defaults to 12345.

    A server will allow at most `max_cores` to be allocated at once. Tasks
    running on the server may allocate one or more of the cores. However, the
    cores used are currently not limited by the executor.
    """
    if max_cores is None:
        max_cores = multiprocessing.cpu_count()
    scheduler = TaskScheduler(max_cores=max_cores, log_manager=log_manager)
    server = Server(scheduler, host, port)
    server.start()
    return server


class Client:
    """Client for connecting to running local backend server.

    Accepts a `host` and `port` to connect to. The `host` defaults to 127.0.0.1
    (localhost) and the port defaults to 12345.

    To connect to a running local backend server:

        >>> c = Client()
        >>> c.connect()

    You can now interact with the local backend server. For example, to get
    the status of a task:

        >>> c.status()

    Remember to close the connection again by calling `close`:

        >>> c.close()

    You may also use the `Client` as a context manager. In this case, the
    client will automatically connect and close the connection at the end of
    the context manager:

    with Client() as c:
        c.status("foo")
    """

    def __init__(self, host="127.0.0.1", port=12345):
        self._host = host
        self._port = port
        self._socket = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args, **kwargs):
        self.close()

    def connect(self):
        """Connect to `host` on `port`."""
        self._socket = socket.create_connection((self._host, self._port))

    def close(self):
        """Close the connection to the server."""
        self._socket.close()

    def submit(self, script, working_dir, env=None, resources=None, dependencies=None):
        """Submit a script to the server.

        Submit `script` as a task to the server. The script will run in
        `working_dir` with the environment variables defined in `env`.

        The task will run after all of its `dependencies` have completed.

        :ivar str script:
            The Bash script to be executed.
        :ivar str working_dir:
            The directory in which to execute the task.
        :ivar dict env:
            A dictionary of environment variables available when the task runs.
        :ivar iterable dependencies:
            An iterable of task identifiers for the tasks' dependencies.
        """
        task_id = _gen_task_id()
        response = self._send_message(
            {
                "type": "submit-task",
                "id": task_id,
                "script": script,
                "working_dir": working_dir,
                "env": env,
                "resources": resources,
                "dependencies": dependencies,
            }
        )
        assert response["type"] == "ok"
        return task_id

    def cancel(self, task_id):
        """Cancel the given task."""
        response = self._send_message({"type": "cancel-task", "id": task_id})
        assert response["type"] == "ok"

    def status(self, task_id):
        """Get the status of the given task."""
        response = self._send_message({"type": "get-status", "id": task_id})
        return LocalStatus[response["status"]]

    def _send_message(self, message):
        self._socket.sendall(bytes(json.dumps(message) + "\n", "utf-8"))
        data = self._socket.recv(8192)
        return json.loads(data.decode("utf-8"))
