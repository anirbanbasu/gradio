"""The main Client class for the Python client."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
import uuid
import warnings
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any, Literal, cast

import httpx
import huggingface_hub
from huggingface_hub import CommitOperationAdd, SpaceHardware, SpaceStage
from huggingface_hub.utils import (
    RepositoryNotFoundError,
    build_hf_headers,
    send_telemetry,
)
from packaging import version

from gradio_client import utils
from gradio_client.compatibility import EndpointV3Compatibility
from gradio_client.data_classes import ParameterInfo
from gradio_client.documentation import document
from gradio_client.exceptions import AppError, AuthenticationError
from gradio_client.utils import (
    Communicator,
    JobStatus,
    Message,
    QueueError,
    ServerMessage,
    Status,
    StatusUpdate,
    Update,
)

DEFAULT_TEMP_DIR = os.environ.get("GRADIO_TEMP_DIR") or str(
    Path(tempfile.gettempdir()) / "gradio"
)


@document("predict", "submit", "view_api", "duplicate", "deploy_discord")
class Client:
    """
    The main Client class for the Python client. This class is used to connect to a remote Gradio app and call its API endpoints.

    Example:
        from gradio_client import Client

        client = Client("abidlabs/whisper-large-v2")  # connecting to a Hugging Face Space
        client.predict("test.mp4", api_name="/predict")
        >> What a nice recording! # returns the result of the remote API call

        client = Client("https://bec81a83-5b5c-471e.gradio.live")  # connecting to a temporary Gradio share URL
        job = client.submit("hello", api_name="/predict")  # runs the prediction in a background thread
        job.result()
        >> 49 # returns the result of the remote API call (blocking call)
    """

    def __init__(
        self,
        src: str,
        hf_token: str | Literal[False] | None = False,
        max_workers: int = 40,
        verbose: bool = True,
        auth: tuple[str, str] | None = None,
        httpx_kwargs: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        download_files: str | Path | Literal[False] = DEFAULT_TEMP_DIR,
        ssl_verify: bool = True,
        _skip_components: bool = True,  # internal parameter to skip values certain components (e.g. State) that do not need to be displayed to users.
        analytics_enabled: bool = True,
    ):
        """
        Parameters:
            src: either the name of the Hugging Face Space to load, (e.g. "abidlabs/whisper-large-v2") or the full URL (including "http" or "https") of the hosted Gradio app to load (e.g. "http://mydomain.com/app" or "https://bec81a83-5b5c-471e.gradio.live/").
            hf_token: optional Hugging Face token to use to access private Spaces. By default, no token is sent to the server. Set `hf_token=None` to use the locally saved token if there is one (warning: only provide a token if you are loading a trusted private Space as the token can be read by the Space you are loading). Find your tokens here: https://huggingface.co/settings/tokens.
            max_workers: maximum number of thread workers that can be used to make requests to the remote Gradio app simultaneously.
            verbose: whether the client should print statements to the console.
            headers: additional headers to send to the remote Gradio app on every request. By default only the HF authorization and user-agent headers are sent. This parameter will override the default headers if they have the same keys.
            download_files: directory where the client should download output files  on the local machine from the remote API. By default, uses the value of the GRADIO_TEMP_DIR environment variable which, if not set by the user, is a temporary directory on your machine. If False, the client does not download files and returns a FileData dataclass object with the filepath on the remote machine instead.
            ssl_verify: if False, skips certificate validation which allows the client to connect to Gradio apps that are using self-signed certificates.
            httpx_kwargs: additional keyword arguments to pass to `httpx.Client`, `httpx.stream`, `httpx.get` and `httpx.post`. This can be used to set timeouts, proxies, http auth, etc.
            analytics_enabled: Whether to allow basic telemetry. If None, will use GRADIO_ANALYTICS_ENABLED environment variable or default to True.
        """
        self.verbose = verbose
        self.hf_token = hf_token
        self.download_files = download_files
        self._skip_components = _skip_components
        self.headers = build_hf_headers(
            token=hf_token,
            library_name="gradio_client",
            library_version=utils.__version__,
        )
        if headers:
            self.headers.update(headers)
        self.ssl_verify = ssl_verify
        self.space_id = None
        self.httpx_kwargs = {} if httpx_kwargs is None else httpx_kwargs
        self.cookies: dict[str, str] = dict(
            (self.httpx_kwargs.pop("cookies", {})) or {}
        )
        if isinstance(self.download_files, (str, Path)):
            if not os.path.exists(self.download_files):
                os.makedirs(self.download_files, exist_ok=True)
            if not os.path.isdir(self.download_files):
                raise ValueError(f"Path: {self.download_files} is not a directory.")
            self.output_dir = str(self.download_files)
        else:
            self.output_dir = DEFAULT_TEMP_DIR

        if src.startswith("http://") or src.startswith("https://"):
            _src = src if src.endswith("/") else src + "/"
        else:
            _src = self._space_name_to_src(src)
            if _src is None:
                raise ValueError(
                    f"Could not find Space: {src}. If it is a private Space, please provide an hf_token."
                )
            self.space_id = src
        self.src = _src
        state = self._get_space_state()
        if state == SpaceStage.BUILDING:
            if self.verbose:
                print("Space is still building. Please wait...")
            while self._get_space_state() == SpaceStage.BUILDING:
                time.sleep(2)  # so we don't get rate limited by the API
                pass
        if state in utils.INVALID_RUNTIME:
            raise ValueError(
                f"The current space is in the invalid state: {state}. "
                "Please contact the owner to fix this."
            )
        if self.verbose:
            print(f"Loaded as API: {self.src} ✔")

        if auth is not None:
            self._login(auth)

        self.config = self._get_config()
        self.protocol: Literal["ws", "sse", "sse_v1", "sse_v2", "sse_v2.1"] = (
            self.config.get("protocol", "ws")
        )
        api_prefix: str = self.config.get("api_prefix", "")
        self.api_prefix = api_prefix.lstrip("/") + "/"
        self.src_prefixed = (
            urllib.parse.urljoin(self.src, self.api_prefix).rstrip("/") + "/"
        )
        self.api_url = urllib.parse.urljoin(self.src_prefixed, utils.API_URL)
        self.sse_url = urllib.parse.urljoin(
            self.src_prefixed,
            utils.SSE_URL_V0 if self.protocol == "sse" else utils.SSE_URL,
        )
        self.heartbeat_url = urllib.parse.urljoin(
            self.src_prefixed, utils.HEARTBEAT_URL
        )
        self.sse_data_url = urllib.parse.urljoin(
            self.src_prefixed,
            utils.SSE_DATA_URL_V0 if self.protocol == "sse" else utils.SSE_DATA_URL,
        )
        self.ws_url = urllib.parse.urljoin(
            self.src_prefixed.replace("http", "ws", 1), utils.WS_URL
        )
        self.upload_url = urllib.parse.urljoin(self.src_prefixed, utils.UPLOAD_URL)
        self.reset_url = urllib.parse.urljoin(self.src_prefixed, utils.RESET_URL)
        self.app_version = version.parse(self.config.get("version", "2.0"))
        self._info = self._get_api_info()
        self.session_hash = str(uuid.uuid4())

        endpoint_class = (
            Endpoint if self.protocol.startswith("sse") else EndpointV3Compatibility
        )
        self.endpoints = {
            dependency.get("id", fn_index): endpoint_class(
                self, dependency.get("id", fn_index), dependency, self.protocol
            )
            for fn_index, dependency in enumerate(self.config["dependencies"])
        }

        # Create a pool of threads to handle the requests
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

        self.analytics_enabled = (
            analytics_enabled or os.getenv("GRADIO_ANALYTICS_ENABLED", "True") == "True"
        )
        if self.analytics_enabled:
            threading.Thread(target=self._telemetry_thread, daemon=True).start()
        self._refresh_heartbeat = threading.Event()
        self._kill_heartbeat = threading.Event()

        self.heartbeat = threading.Thread(target=self._stream_heartbeat, daemon=True)
        self.heartbeat.start()

        self.stream_open = False
        self.streaming_future: Future | None = None
        self.pending_messages_per_event: dict[str, list[Message | None]] = {}
        self.pending_event_ids: set[str] = set()

    def close(self):
        self._kill_heartbeat.set()
        self.heartbeat.join(timeout=1)

    def _stream_heartbeat(self):
        while True:
            url = self.heartbeat_url.format(session_hash=self.session_hash)
            try:
                httpx_kwargs = self.httpx_kwargs.copy()
                httpx_kwargs.setdefault("timeout", 20)
                with httpx.stream(
                    "GET",
                    url,
                    headers=self.headers,
                    cookies=self.cookies,
                    verify=self.ssl_verify,
                    **httpx_kwargs,
                ) as response:
                    for _ in response.iter_lines():
                        if self._refresh_heartbeat.is_set():
                            self._refresh_heartbeat.clear()
                            break
                        if self._kill_heartbeat.is_set():
                            return
            except httpx.TransportError:
                return

    def stream_messages(
        self, protocol: Literal["sse_v1", "sse_v2", "sse_v2.1", "sse_v3"]
    ) -> None:
        try:
            httpx_kwargs = self.httpx_kwargs.copy()
            httpx_kwargs.setdefault("timeout", httpx.Timeout(timeout=None))
            with httpx.Client(
                verify=self.ssl_verify,
                **httpx_kwargs,
            ) as client:
                with client.stream(
                    "GET",
                    self.sse_url,
                    params={"session_hash": self.session_hash},
                    headers=self.headers,
                    cookies=self.cookies,
                ) as response:
                    buffer = b""
                    for chunk in response.iter_bytes():
                        buffer += chunk
                        while b"\n\n" in buffer:
                            line, buffer = buffer.split(b"\n\n", 1)
                            line = line.decode("utf-8").rstrip("\n")
                            if not len(line):
                                continue
                            if line.startswith("data:"):
                                resp = json.loads(line[5:])
                                if resp["msg"] == ServerMessage.heartbeat:
                                    continue
                                elif (
                                    resp.get("message", "")
                                    == ServerMessage.server_stopped
                                ):
                                    for (
                                        pending_messages
                                    ) in self.pending_messages_per_event.values():
                                        pending_messages.append(resp)
                                    return
                                elif resp["msg"] == ServerMessage.close_stream:
                                    self.stream_open = False
                                    return
                                event_id = resp["event_id"]
                                if event_id not in self.pending_messages_per_event:
                                    self.pending_messages_per_event[event_id] = []
                                self.pending_messages_per_event[event_id].append(resp)
                                if resp["msg"] == ServerMessage.process_completed:
                                    self.pending_event_ids.remove(event_id)
                                if (
                                    len(self.pending_event_ids) == 0
                                    and protocol != "sse_v3"
                                ):
                                    self.stream_open = False
                                    return
                            else:
                                raise ValueError(f"Unexpected SSE line: '{line}'")
        except BaseException as e:
            # If the job is cancelled the stream will close so we
            # should not raise this httpx exception that comes from the
            # stream abruply closing
            if isinstance(e, httpx.RemoteProtocolError):
                return
            import traceback

            traceback.print_exc()
            raise e

    def send_data(self, data, hash_data, protocol, request_headers):
        headers = self.add_zero_gpu_headers(self.headers)
        if request_headers is not None:
            headers = {**request_headers, **headers}
        req = httpx.post(
            self.sse_data_url,
            json={**data, **hash_data},
            headers=headers,
            cookies=self.cookies,
            verify=self.ssl_verify,
            **self.httpx_kwargs,
        )
        if req.status_code == 503:
            raise QueueError("Queue is full! Please try again.")
        req.raise_for_status()
        resp = req.json()
        event_id = resp["event_id"]

        if not self.stream_open:
            self.stream_open = True

            def open_stream():
                return self.stream_messages(protocol)

            def close_stream(_):
                self.stream_open = False
                for _, pending_messages in self.pending_messages_per_event.items():
                    pending_messages.append(None)

            if self.streaming_future is None or self.streaming_future.done():
                self.streaming_future = self.executor.submit(open_stream)
                self.streaming_future.add_done_callback(close_stream)

        return event_id

    @classmethod
    def duplicate(
        cls,
        from_id: str,
        to_id: str | None = None,
        hf_token: str | Literal[False] | None = False,
        private: bool = True,
        hardware: Literal[
            "cpu-basic",
            "cpu-upgrade",
            "t4-small",
            "t4-medium",
            "a10g-small",
            "a10g-large",
            "a100-large",
        ]
        | SpaceHardware
        | None = None,
        secrets: dict[str, str] | None = None,
        sleep_timeout: int = 5,
        max_workers: int = 40,
        verbose: bool = True,
    ):
        """
        Duplicates a Hugging Face Space under your account and returns a Client object
        for the new Space. No duplication is created if the Space already exists in your
        account (to override this, provide a new name for the new Space using `to_id`).
        To use this method, you must provide an `hf_token` or be logged in via the Hugging
        Face Hub CLI.

        The new Space will be private by default and use the same hardware as the original
        Space. This can be changed by using the `private` and `hardware` parameters. For
        hardware upgrades (beyond the basic CPU tier), you may be required to provide
        billing information on Hugging Face: https://huggingface.co/settings/billing

        Parameters:
            from_id: The name of the Hugging Face Space to duplicate in the format "{username}/{space_id}", e.g. "gradio/whisper".
            to_id: The name of the new Hugging Face Space to create, e.g. "abidlabs/whisper-duplicate". If not provided, the new Space will be named "{your_HF_username}/{space_id}".
            hf_token: optional Hugging Face token to use to duplicating private Spaces. By default, no token is sent to the server. Set `hf_token=None` to use the locally saved token if there is one. Find your tokens here: https://huggingface.co/settings/tokens.
            private: Whether the new Space should be private (True) or public (False). Defaults to True.
            hardware: The hardware tier to use for the new Space. Defaults to the same hardware tier as the original Space. Options include "cpu-basic", "cpu-upgrade", "t4-small", "t4-medium", "a10g-small", "a10g-large", "a100-large", subject to availability.
            secrets: A dictionary of (secret key, secret value) to pass to the new Space. Defaults to None. Secrets are only used when the Space is duplicated for the first time, and are not updated if the duplicated Space already exists.
            sleep_timeout: The number of minutes after which the duplicate Space will be puased if no requests are made to it (to minimize billing charges). Defaults to 5 minutes.
            max_workers: The maximum number of thread workers that can be used to make requests to the remote Gradio app simultaneously.
            verbose: Whether the client should print statements to the console.
        Example:
            import os
            from gradio_client import Client
            HF_TOKEN = os.environ.get("HF_TOKEN")
            client = Client.duplicate("abidlabs/whisper", hf_token=HF_TOKEN)
            client.predict("audio_sample.wav")
            >> "This is a test of the whisper speech recognition model."
        """
        try:
            original_info = huggingface_hub.get_space_runtime(from_id, token=hf_token)
        except RepositoryNotFoundError as rnfe:
            raise ValueError(
                f"Could not find Space: {from_id}. If it is a private Space, please provide an `hf_token`."
            ) from rnfe
        if to_id:
            if "/" in to_id:
                to_id = to_id.split("/")[1]
            space_id = huggingface_hub.get_full_repo_name(to_id, token=hf_token)
        else:
            space_id = huggingface_hub.get_full_repo_name(
                from_id.split("/")[1], token=hf_token
            )
        try:
            huggingface_hub.get_space_runtime(space_id, token=hf_token)
            if verbose:
                print(
                    f"Using your existing Space: {utils.SPACE_URL.format(space_id)} 🤗"
                )
            if secrets is not None:
                warnings.warn(
                    "Secrets are only used when the Space is duplicated for the first time, and are not updated if the duplicated Space already exists."
                )
        except RepositoryNotFoundError:
            if verbose:
                print(f"Creating a duplicate of {from_id} for your own use... 🤗")
            huggingface_hub.duplicate_space(
                from_id=from_id,
                to_id=space_id,
                token=hf_token,
                exist_ok=True,
                private=private,
            )
            if secrets is not None:
                for key, value in secrets.items():
                    huggingface_hub.add_space_secret(
                        space_id, key, value, token=hf_token
                    )
            if verbose:
                print(f"Created new Space: {utils.SPACE_URL.format(space_id)}")
        current_info = huggingface_hub.get_space_runtime(space_id, token=hf_token)
        current_hardware = (
            current_info.hardware or huggingface_hub.SpaceHardware.CPU_BASIC
        )
        hardware = hardware or original_info.hardware
        if current_hardware != hardware:
            huggingface_hub.request_space_hardware(space_id, hardware, token=hf_token)  # type: ignore
            print(
                f"-------\nNOTE: this Space uses upgraded hardware: {hardware}... see billing info at https://huggingface.co/settings/billing\n-------"
            )
        # Setting a timeout only works if the hardware is not basic
        # so set it here after the hardware has been requested
        if hardware != huggingface_hub.SpaceHardware.CPU_BASIC:
            utils.set_space_timeout(
                space_id, hf_token=hf_token, timeout_in_seconds=sleep_timeout * 60
            )
        if verbose:
            print("")
        client = cls(
            space_id, hf_token=hf_token, max_workers=max_workers, verbose=verbose
        )
        return client

    def _get_space_state(self):
        if not self.space_id:
            return None
        info = huggingface_hub.get_space_runtime(self.space_id, token=self.hf_token)
        return info.stage

    def predict(
        self,
        *args,
        api_name: str | None = None,
        fn_index: int | None = None,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> Any:
        """
        Calls the Gradio API and returns the result (this is a blocking call). Arguments can be provided as positional arguments or as keyword arguments (latter is recommended).

        Parameters:
            args: The positional arguments to pass to the remote API endpoint. The order of the arguments must match the order of the inputs in the Gradio app.
            api_name: The name of the API endpoint to call starting with a leading slash, e.g. "/predict". Does not need to be provided if the Gradio app has only one named API endpoint.
            fn_index: As an alternative to api_name, this parameter takes the index of the API endpoint to call, e.g. 0. Both api_name and fn_index can be provided, but if they conflict, api_name will take precedence.
            kwargs: The keyword arguments to pass to the remote API endpoint.
            headers: Additional headers to send to the remote Gradio app on this request. This parameter will overrides the headers provided in the Client constructor if they have the same keys.
        Returns:
            The result of the API call. Will be a Tuple if the API has multiple outputs.
        Example:
            from gradio_client import Client
            client = Client(src="gradio/calculator")
            client.predict(5, "add", 4, api_name="/predict")
            >> 9.0
        """
        self._infer_fn_index(api_name, fn_index)
        return self.submit(
            *args, api_name=api_name, fn_index=fn_index, headers=headers, **kwargs
        ).result()

    def new_helper(
        self, fn_index: int, headers: dict[str, str] | None = None
    ) -> Communicator:
        return Communicator(
            Lock(),
            JobStatus(),
            self.endpoints[fn_index].process_predictions,
            self.reset_url,
            request_headers=headers,
        )

    def submit(
        self,
        *args,
        api_name: str | None = None,
        fn_index: int | None = None,
        headers: dict[str, str] | None = None,
        result_callbacks: Callable | list[Callable] | None = None,
        **kwargs,
    ) -> Job:
        """
        Creates and returns a Job object which calls the Gradio API in a background thread. The job can be used to retrieve the status and result of the remote API call.
         Arguments can be provided as positional arguments or as keyword arguments (latter is recommended).

        Parameters:
            args: The arguments to pass to the remote API. The order of the arguments must match the order of the inputs in the Gradio app.
            api_name: The name of the API endpoint to call starting with a leading slash, e.g. "/predict". Does not need to be provided if the Gradio app has only one named API endpoint.
            fn_index: As an alternative to api_name, this parameter takes the index of the API endpoint to call, e.g. 0. Both api_name and fn_index can be provided, but if they conflict, api_name will take precedence.
            result_callbacks: A callback function, or list of callback functions, to be called when the result is ready. If a list of functions is provided, they will be called in order. The return values from the remote API are provided as separate parameters into the callback. If None, no callback will be called.
            kwargs: The keyword arguments to pass to the remote API endpoint.
            headers: Additional headers to send to the remote Gradio app on this request. This parameter will overrides the headers provided in the Client constructor if they have the same keys.
        Returns:
            A Job object that can be used to retrieve the status and result of the remote API call.
        Example:
            from gradio_client import Client
            client = Client(src="gradio/calculator")
            job = client.submit(5, "add", 4, api_name="/predict")
            job.status()
            >> <Status.STARTING: 'STARTING'>
            job.result()  # blocking call
            >> 9.0
        """
        inferred_fn_index = self._infer_fn_index(api_name, fn_index)

        endpoint = self.endpoints[inferred_fn_index]

        if isinstance(endpoint, Endpoint):
            args = utils.construct_args(endpoint.parameters_info, args, kwargs)

        helper = None
        if endpoint.protocol in (
            "ws",
            "sse",
            "sse_v1",
            "sse_v2",
            "sse_v2.1",
            "sse_v3",
        ):
            helper = self.new_helper(inferred_fn_index, headers=headers)
            end_to_end_fn = endpoint.make_end_to_end_fn(helper)
        else:
            end_to_end_fn = cast(EndpointV3Compatibility, endpoint).make_end_to_end_fn(
                None
            )
        future = self.executor.submit(end_to_end_fn, *args)

        cancel_fn = endpoint.make_cancel(helper)

        job = Job(
            future,
            communicator=helper,
            verbose=self.verbose,
            space_id=self.space_id,
            _cancel_fn=cancel_fn,
        )

        if result_callbacks:
            if isinstance(result_callbacks, Callable):
                result_callbacks = [result_callbacks]

            def create_fn(callback) -> Callable:
                def fn(future):
                    if isinstance(future.result(), tuple):
                        callback(*future.result())
                    else:
                        callback(future.result())

                return fn

            for callback in result_callbacks:
                job.add_done_callback(create_fn(callback))

        return job

    def _get_api_info(self):
        api_info_url = urllib.parse.urljoin(self.src_prefixed, utils.RAW_API_INFO_URL)
        if self.app_version > version.Version("3.36.1"):
            r = httpx.get(
                api_info_url,
                headers=self.headers,
                cookies=self.cookies,
                verify=self.ssl_verify,
                **self.httpx_kwargs,
            )
            if r.is_success:
                info = r.json()
            else:
                raise ValueError(f"Could not fetch api info for {self.src}: {r.text}")
        else:
            fetch = httpx.post(
                utils.SPACE_FETCHER_URL,
                json={
                    "config": json.dumps(self.config),
                    "serialize": False,
                },
                headers=self.headers,
                cookies=self.cookies,
                verify=self.ssl_verify,
                **self.httpx_kwargs,
            )
            if fetch.is_success:
                info = fetch.json()["api"]
            else:
                raise ValueError(
                    f"Could not fetch api info for {self.src}: {fetch.text}"
                )
        info["named_endpoints"] = {
            a: e for a, e in info["named_endpoints"].items() if e.pop("show_api", True)
        }
        info["unnamed_endpoints"] = {
            a: e
            for a, e in info["unnamed_endpoints"].items()
            if e.pop("show_api", True)
        }
        return info

    def view_api(
        self,
        all_endpoints: bool | None = None,
        print_info: bool = True,
        return_format: Literal["dict", "str"] | None = None,
    ) -> dict | str | None:
        """
        Prints the usage info for the API. If the Gradio app has multiple API endpoints, the usage info for each endpoint will be printed separately. If return_format="dict" the info is returned in dictionary format, as shown in the example below.

        Parameters:
            all_endpoints: If True, prints information for both named and unnamed endpoints in the Gradio app. If False, will only print info about named endpoints. If None (default), will print info about named endpoints, unless there aren't any -- in which it will print info about unnamed endpoints.
            print_info: If True, prints the usage info to the console. If False, does not print the usage info.
            return_format: If None, nothing is returned. If "str", returns the same string that would be printed to the console. If "dict", returns the usage info as a dictionary that can be programmatically parsed, and *all endpoints are returned in the dictionary* regardless of the value of `all_endpoints`. The format of the dictionary is in the docstring of this method.
        Example:
            from gradio_client import Client
            client = Client(src="gradio/calculator")
            client.view_api(return_format="dict")
            >> {
                'named_endpoints': {
                    '/predict': {
                        'parameters': [
                            {
                                'label': 'num1',
                                'python_type': 'int | float',
                                'type_description': 'numeric value',
                                'component': 'Number',
                                'example_input': '5'
                            },
                            {
                                'label': 'operation',
                                'python_type': 'str',
                                'type_description': 'string value',
                                'component': 'Radio',
                                'example_input': 'add'
                            },
                            {
                                'label': 'num2',
                                'python_type': 'int | float',
                                'type_description': 'numeric value',
                                'component': 'Number',
                                'example_input': '5'
                            },
                        ],
                        'returns': [
                            {
                                'label': 'output',
                                'python_type': 'int | float',
                                'type_description': 'numeric value',
                                'component': 'Number',
                            },
                        ]
                    },
                    '/flag': {
                        'parameters': [
                            ...
                            ],
                        'returns': [
                            ...
                            ]
                        }
                    }
                }
                'unnamed_endpoints': {
                    2: {
                        'parameters': [
                            ...
                            ],
                        'returns': [
                            ...
                            ]
                        }
                    }
                }
            }

        """
        num_named_endpoints = len(self._info["named_endpoints"])
        num_unnamed_endpoints = len(self._info["unnamed_endpoints"])
        if num_named_endpoints == 0 and all_endpoints is None:
            all_endpoints = True

        human_info = "Client.predict() Usage Info\n---------------------------\n"
        human_info += f"Named API endpoints: {num_named_endpoints}\n"

        for api_name, endpoint_info in self._info["named_endpoints"].items():
            human_info += self._render_endpoints_info(api_name, endpoint_info)

        if all_endpoints:
            human_info += f"\nUnnamed API endpoints: {num_unnamed_endpoints}\n"
            for fn_index, endpoint_info in self._info["unnamed_endpoints"].items():
                # When loading from json, the fn_indices are read as strings
                # because json keys can only be strings
                human_info += self._render_endpoints_info(int(fn_index), endpoint_info)
        elif num_unnamed_endpoints > 0:
            human_info += f"\nUnnamed API endpoints: {num_unnamed_endpoints}, to view, run Client.view_api(all_endpoints=True)\n"

        if print_info:
            print(human_info)
        if return_format == "str":
            return human_info
        elif return_format == "dict":
            return self._info

    def reset_session(self) -> None:
        self.session_hash = str(uuid.uuid4())
        self._refresh_heartbeat.set()

    def add_zero_gpu_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """
        Adds the x-ip-token header to the headers dictionary to pass it to a Zero-GPU Space. This allows a user's
        ZeroGPU quota to be tracked and used by the underlying Space. For the x-ip-token header to be present,
        this method needs to be called when a Gradio app's LocalContext is defined. i.e. this method
        cannot be called when the Gradio Client is instantiated, but must be called from inside a Gradio app's
        prediction function.
        """
        if not self.space_id:
            return headers
        try:
            from gradio.context import LocalContext
        except (
            ImportError
        ):  # this is not running within a Gradio app as Gradio is not installed
            return headers
        request = LocalContext.request.get()
        if request and hasattr(request, "headers") and "x-ip-token" in request.headers:
            headers["x-ip-token"] = request.headers["x-ip-token"]
        return headers

    def _render_endpoints_info(
        self,
        name_or_index: str | int,
        endpoints_info: dict[str, list[ParameterInfo]],
    ) -> str:
        parameter_info = endpoints_info["parameters"]
        parameter_names = [
            p.get("parameter_name") or p["label"] for p in parameter_info
        ]
        parameter_names = [utils.sanitize_parameter_names(p) for p in parameter_names]
        rendered_parameters = ", ".join(parameter_names)
        if rendered_parameters:
            rendered_parameters = rendered_parameters + ", "
        return_values = [p["label"] for p in endpoints_info["returns"]]
        return_values = [utils.sanitize_parameter_names(r) for r in return_values]
        rendered_return_values = ", ".join(return_values)
        if len(return_values) > 1:
            rendered_return_values = f"({rendered_return_values})"

        if isinstance(name_or_index, str):
            final_param = f'api_name="{name_or_index}"'
        elif isinstance(name_or_index, int):
            final_param = f"fn_index={name_or_index}"
        else:
            raise ValueError("name_or_index must be a string or integer")

        human_info = f"\n - predict({rendered_parameters}{final_param}) -> {rendered_return_values}\n"
        human_info += "    Parameters:\n"
        if parameter_info:
            for info in parameter_info:
                desc = (
                    f" ({info['python_type']['description']})"
                    if info["python_type"].get("description")
                    else ""
                )
                default_value = info.get("parameter_default")
                default_value = utils.traverse(
                    default_value,
                    lambda x: f'handle_file("{x["url"]}")',
                    utils.is_file_obj_with_meta,
                )
                default_info = (
                    "(required)"
                    if not info.get("parameter_has_default", False)
                    else f"(not required, defaults to:   {default_value})"
                )
                type_ = info["python_type"]["type"]
                if info.get("parameter_has_default", False) and default_value is None:
                    type_ += " | None"
                human_info += f"     - [{info['component']}] {utils.sanitize_parameter_names(info.get('parameter_name') or info['label'])}: {type_} {default_info} {desc} \n"
        else:
            human_info += "     - None\n"
        human_info += "    Returns:\n"
        if endpoints_info["returns"]:
            for info in endpoints_info["returns"]:
                desc = (
                    f" ({info['python_type']['description']})"
                    if info["python_type"].get("description")
                    else ""
                )
                type_ = info["python_type"]["type"]
                human_info += f"     - [{info['component']}] {utils.sanitize_parameter_names(info['label'])}: {type_}{desc} \n"
        else:
            human_info += "     - None\n"

        return human_info

    def __repr__(self):
        return self.view_api(print_info=False, return_format="str")

    def __str__(self):
        return self.view_api(print_info=False, return_format="str")

    def _telemetry_thread(self) -> None:
        # Disable telemetry by setting the env variable HF_HUB_DISABLE_TELEMETRY=1
        data = {
            "src": self.src,
        }
        try:
            send_telemetry(
                topic="py_client/initiated",
                library_name="gradio_client",
                library_version=utils.__version__,
                user_agent=data,
            )
        except Exception:
            pass

    def _infer_fn_index(self, api_name: str | None, fn_index: int | None) -> int:
        inferred_fn_index = None
        if api_name is not None:
            for i, d in enumerate(self.config["dependencies"]):
                config_api_name = d.get("api_name")
                if config_api_name is None or config_api_name is False:
                    continue
                if "/" + config_api_name == api_name:
                    inferred_fn_index = d.get("id", i)
                    break
            else:
                error_message = f"Cannot find a function with `api_name`: {api_name}."
                if not api_name.startswith("/"):
                    error_message += " Did you mean to use a leading slash?"
                raise ValueError(error_message)
        elif fn_index is not None:
            inferred_fn_index = fn_index
            if (
                inferred_fn_index not in self.endpoints
                or not self.endpoints[inferred_fn_index].is_valid
            ):
                raise ValueError(f"Invalid function index: {fn_index}.")
        else:
            valid_endpoints = [
                e
                for e in self.endpoints.values()
                if e.is_valid
                and e.api_name is not None
                and e.backend_fn is not None
                and e.show_api
            ]
            if len(valid_endpoints) == 1:
                inferred_fn_index = valid_endpoints[0].fn_index
            else:
                raise ValueError(
                    "This Gradio app might have multiple endpoints. Please specify an `api_name` or `fn_index`"
                )
        return inferred_fn_index

    def __del__(self):
        if hasattr(self, "executor"):
            self.executor.shutdown(wait=True)

    def _space_name_to_src(self, space) -> str | None:
        return huggingface_hub.space_info(space, token=self.hf_token).host  # type: ignore

    def _login(self, auth: tuple[str, str]):
        """
        Logs in to `utils.LOGIN_URL` using provided `auth` credentials.
        Warning: This method overwrites `self.cookies`.
        """
        resp = httpx.post(
            urllib.parse.urljoin(self.src, utils.LOGIN_URL),
            data={"username": auth[0], "password": auth[1]},
            verify=self.ssl_verify,
            **self.httpx_kwargs,
        )
        if not resp.is_success:
            if resp.status_code == 401:
                raise AuthenticationError(
                    f"Could not login to {self.src}. Invalid credentials."
                )
            else:
                raise ValueError(f"Could not login to {self.src}.")
        self.cookies = {
            name: value for name, value in resp.cookies.items() if value is not None
        }

    def _get_config(self) -> dict:
        r = httpx.get(
            urllib.parse.urljoin(self.src, utils.CONFIG_URL),
            headers=self.headers,
            cookies=self.cookies,
            verify=self.ssl_verify,
            **self.httpx_kwargs,
        )
        if r.is_success:
            # Cookies are sometimes needed to correctly route requests if the Gradio app is
            # running on multiple replicas e.g. using cookie session-affinity in Kubernetes.
            # This approach attaches cookies from the first response to subsequent requests
            # without overriding existing cookies.
            new_cookies = {
                name: value
                for name, value in r.cookies.items()
                if value is not None and name not in self.cookies
            }
            self.cookies.update(new_cookies)
            return r.json()
        elif r.status_code == 401:
            raise AuthenticationError(
                f"Could not load {self.src} as credentials were not provided. Please login."
            )
        elif r.status_code == 429:
            raise utils.TooManyRequestsError(
                "Too many requests to the API, please try again later."
            ) from None
        else:  # to support older versions of Gradio
            r = httpx.get(
                self.src,
                headers=self.headers,
                cookies=self.cookies,
                verify=self.ssl_verify,
                **self.httpx_kwargs,
            )
            if not r.is_success:
                raise ValueError(f"Could not fetch config for {self.src}")
            # some basic regex to extract the config
            result = re.search(r"window.gradio_config = (.*?);[\s]*</script>", r.text)
            try:
                config = json.loads(result.group(1))  # type: ignore
            except AttributeError as ae:
                raise ValueError(
                    f"Could not get Gradio config from: {self.src}"
                ) from ae
            if "allow_flagging" in config:
                raise ValueError(
                    "Gradio 2.x is not supported by this client. Please upgrade your Gradio app to Gradio 3.x or higher."
                )
            return config

    def deploy_discord(
        self,
        discord_bot_token: str | None = None,
        api_names: list[str | tuple[str, str]] | None = None,
        to_id: str | None = None,
        hf_token: str | Literal[False] | None = False,
        private: bool = False,
    ):
        """
        Deploy the upstream app as a discord bot. Currently only supports gr.ChatInterface.
        Parameters:
            discord_bot_token: This is the "password" needed to be able to launch the bot. Users can get a token by creating a bot app on the discord website. If run the method without specifying a token, the space will explain how to get one. See here: https://huggingface.co/spaces/freddyaboulton/test-discord-bot-v1.
            api_names: The api_names of the app to turn into bot commands. This parameter currently has no effect as ChatInterface only has one api_name ('/chat').
            to_id: The name of the space hosting the discord bot. If None, the name will be gradio-discord-bot-{random-substring}
            hf_token: HF api token with write priviledges in order to upload the files to HF space. Can be ommitted if logged in via the HuggingFace CLI, unless the upstream space is private. Obtain from: https://huggingface.co/settings/token
            private: Whether the space hosting the discord bot is private. The visibility of the discord bot itself is set via the discord website. See https://huggingface.co/spaces/freddyaboulton/test-discord-bot-v1
        """
        warnings.warn(
            "This method is deprecated and may be removed in the future. Please see the documentation on how to create a discord bot with Gradio: https://www.gradio.app/guides/creating-a-discord-bot-from-a-gradio-app"
        )
        if self.config["mode"] == "chat_interface" and not api_names:
            api_names = [("chat", "chat")]

        valid_list = isinstance(api_names, list) and (
            isinstance(n, str)
            or (
                isinstance(n, tuple) and isinstance(n[0], str) and isinstance(n[1], str)
            )
            for n in api_names
        )
        if api_names is None or not valid_list:
            raise ValueError(
                f"Each entry in api_names must be either a string or a tuple of strings. Received {api_names}"
            )
        if len(api_names) != 1:
            raise ValueError("Currently only one api_name can be deployed to discord.")

        for i, name in enumerate(api_names):
            if isinstance(name, str):
                api_names[i] = (name, name)

        fn = next(
            (
                ep
                for ep in self.endpoints.values()
                if ep.api_name == f"/{api_names[0][0]}"
            ),
            None,
        )
        if not fn:
            raise ValueError(
                f"api_name {api_names[0][0]} not present in {self.space_id or self.src}"
            )
        inputs = [inp for inp in fn.input_component_types if not inp.skip]
        outputs = [inp for inp in fn.input_component_types if not inp.skip]
        if not inputs == ["textbox"] and outputs == ["textbox"]:
            raise ValueError(
                "Currently only api_names with a single textbox as input and output are supported. "
                f"Received {inputs} and {outputs}"
            )

        is_private = False
        if self.space_id:
            is_private = huggingface_hub.space_info(self.space_id).private
            if is_private and not hf_token:
                raise ValueError(
                    f"Since {self.space_id} is private, you must explicitly pass in hf_token "
                    "so that it can be added as a secret in the discord bot space."
                )

        if to_id:
            if "/" in to_id:
                to_id = to_id.split("/")[1]
            space_id = huggingface_hub.get_full_repo_name(to_id, token=hf_token)
        else:
            if self.space_id:
                space_id = f"{self.space_id.split('/')[1]}-gradio-discord-bot"
            else:
                space_id = f"gradio-discord-bot-{secrets.token_hex(4)}"
            space_id = huggingface_hub.get_full_repo_name(space_id, token=hf_token)

        api = huggingface_hub.HfApi()

        try:
            huggingface_hub.space_info(space_id)
            first_upload = False
        except huggingface_hub.utils.RepositoryNotFoundError:
            first_upload = True

        huggingface_hub.create_repo(
            space_id,
            repo_type="space",
            space_sdk="gradio",
            token=hf_token,
            exist_ok=True,
            private=private,
        )
        if first_upload:
            huggingface_hub.metadata_update(
                repo_id=space_id,
                repo_type="space",
                metadata={"tags": ["gradio-discord-bot"]},
            )

        with open(
            str(Path(__file__).parent / "templates" / "discord_chat.py"),
            encoding="utf-8",
        ) as f:
            app = f.read()
        app = app.replace("<<app-src>>", self.src)
        app = app.replace("<<api-name>>", api_names[0][0])
        app = app.replace("<<command-name>>", api_names[0][1])

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, encoding="utf-8"
        ) as app_file:
            with tempfile.NamedTemporaryFile(mode="w", delete=False) as requirements:
                app_file.write(app)
                requirements.write("\n".join(["discord.py==2.3.1"]))

        operations = [
            CommitOperationAdd(path_in_repo="app.py", path_or_fileobj=app_file.name),
            CommitOperationAdd(
                path_in_repo="requirements.txt", path_or_fileobj=requirements.name
            ),
        ]

        api.create_commit(
            repo_id=space_id,
            commit_message="Deploy Discord Bot",
            repo_type="space",
            operations=operations,
            token=hf_token,
        )

        if discord_bot_token:
            huggingface_hub.add_space_secret(
                space_id, "DISCORD_TOKEN", discord_bot_token, token=hf_token
            )
        if is_private:
            huggingface_hub.add_space_secret(
                space_id,
                "HF_TOKEN",
                hf_token,  # type: ignore
                token=hf_token,
            )

        url = f"https://huggingface.co/spaces/{space_id}"
        print(f"See your discord bot here! {url}")
        return url


@dataclass
class ComponentApiType:
    skip: bool
    value_is_file: bool
    is_state: bool


@dataclass
class ReplaceMe:
    index: int


class Endpoint:
    """Helper class for storing all the information about a single API endpoint."""

    def __init__(
        self, client: Client, fn_index: int, dependency: dict, protocol: str = "sse_v1"
    ):
        self.client: Client = client
        self.fn_index = fn_index
        self.dependency = dependency
        api_name = dependency.get("api_name")
        self.api_name: str | Literal[False] | None = (
            "/" + api_name if isinstance(api_name, str) else api_name
        )
        self._info = self.client._info
        self.protocol = protocol
        self.input_component_types = [
            self._get_component_type(id_) for id_ in dependency["inputs"]
        ]
        self.output_component_types = [
            self._get_component_type(id_) for id_ in dependency["outputs"]
        ]
        self.parameters_info = self._get_parameters_info()
        self.root_url = self.client.src_prefixed

        # Disallow hitting endpoints that the Gradio app has disabled
        self.is_valid = self.api_name is not False
        self.backend_fn = dependency.get("backend_fn")
        self.show_api = dependency.get("show_api")

    def _get_component_type(self, component_id: int):
        component = next(
            i for i in self.client.config["components"] if i["id"] == component_id
        )
        skip_api = component.get("skip_api", component["type"] in utils.SKIP_COMPONENTS)
        return ComponentApiType(
            skip_api,
            self.value_is_file(component),
            component["type"] == "state",
        )

    def _get_parameters_info(self) -> list[ParameterInfo] | None:
        if self.api_name in self._info["named_endpoints"]:
            return self._info["named_endpoints"][self.api_name]["parameters"]
        return None

    @staticmethod
    def value_is_file(component: dict) -> bool:
        # This is still hacky as it does not tell us which part of the payload is a file.
        # If a component has a complex payload, part of which is a file, this will simply
        # return True, which means that all parts of the payload will be uploaded as files
        # if they are valid file paths. We will deprecate this 1.0.
        if "api_info" not in component:
            return False
        return utils.value_is_file(component["api_info"])

    def __repr__(self):
        return f"Endpoint src: {self.client.src}, api_name: {self.api_name}, fn_index: {self.fn_index}"

    def __str__(self):
        return self.__repr__()

    def make_end_to_end_fn(self, helper: Communicator):
        _predict = self.make_predict(helper)

        def _inner(*data):
            if not self.is_valid:
                raise utils.InvalidAPIEndpointError()

            if self.client._skip_components:
                data = self.insert_empty_state(*data)
            data = self.process_input_files(*data)
            predictions = _predict(*data)
            predictions = self.process_predictions(*predictions)

            # Append final output only if not already present
            # for consistency between generators and not generators
            if helper:
                with helper.lock:
                    if not helper.job.outputs:
                        helper.job.outputs.append(predictions)
            return predictions

        return _inner

    def make_cancel(
        self,
        helper: Communicator | None,
    ):
        if helper is None:
            return
        if self.client.app_version > version.Version("4.29.0"):
            url = urllib.parse.urljoin(self.client.src_prefixed, utils.CANCEL_URL)

            # The event_id won't be set on the helper until later
            # so need to create the data in a function that's run at cancel time
            def post_data():
                return {
                    "fn_index": self.fn_index,
                    "session_hash": self.client.session_hash,
                    "event_id": helper.event_id,
                }

            cancel_msg = None
            cancellable = True
        else:
            candidates: list[tuple[int, list[int]]] = []
            for i, dep in enumerate(self.client.config["dependencies"]):
                if self.fn_index in dep["cancels"]:
                    candidates.append(
                        (i, [d for d in dep["cancels"] if d != self.fn_index])
                    )

            fn_index, other_cancelled = (
                min(candidates, key=lambda x: len(x[1])) if candidates else (None, None)
            )
            cancellable = fn_index is not None
            cancel_msg = None
            if cancellable and other_cancelled:
                other_api_names = [
                    "/" + self.client.config["dependencies"][i].get("api_name")
                    for i in other_cancelled
                ]
                cancel_msg = (
                    f"Cancelled this job will also cancel any jobs for {', '.join(other_api_names)} "
                    "that are currently running."
                )
            elif not cancellable:
                cancel_msg = (
                    "Cancelling this job will not stop the server from running. "
                    "To fix this, an event must be added to the upstream app that explicitly cancels this one or "
                    "the upstream app must be running Gradio 4.29.0 and greater."
                )

            def post_data():
                return {
                    "data": [],
                    "fn_index": fn_index,
                    "session_hash": self.client.session_hash,
                }

            url = self.client.api_url

        def _cancel():
            if cancel_msg:
                warnings.warn(cancel_msg)
            if cancellable:
                httpx.post(
                    url,
                    json=post_data(),
                    headers=self.client.headers,
                    cookies=self.client.cookies,
                    verify=self.client.ssl_verify,
                    **self.client.httpx_kwargs,
                )

        return _cancel

    def make_predict(self, helper: Communicator):
        def _predict(*data) -> tuple:
            data = {
                "data": data,
                "fn_index": self.fn_index,
                "session_hash": self.client.session_hash,
            }

            hash_data = {
                "fn_index": self.fn_index,
                "session_hash": self.client.session_hash,
            }

            if self.protocol == "sse":
                result = self._sse_fn_v0(data, hash_data, helper)  # type: ignore
            elif self.protocol in ("sse_v1", "sse_v2", "sse_v2.1", "sse_v3"):
                event_id = self.client.send_data(
                    data, hash_data, self.protocol, helper.request_headers
                )
                self.client.pending_event_ids.add(event_id)
                self.client.pending_messages_per_event[event_id] = []
                helper.event_id = event_id
                result = self._sse_fn_v1plus(helper, event_id, self.protocol)
            else:
                raise ValueError(f"Unsupported protocol: {self.protocol}")

            if "error" in result:
                if result["error"] is None:
                    raise AppError(
                        "The upstream Gradio app has raised an exception but has not enabled "
                        "verbose error reporting. To enable, set show_error=True in launch()."
                    )
                else:
                    message = result.pop("error")
                    raise AppError(message=message, **result)

            try:
                output = result["data"]
            except KeyError as ke:
                is_public_space = (
                    self.client.space_id
                    and not huggingface_hub.space_info(self.client.space_id).private
                )
                if "error" in result and "429" in result["error"] and is_public_space:
                    raise utils.TooManyRequestsError(
                        f"Too many requests to the API, please try again later. To avoid being rate-limited, "
                        f"please duplicate the Space using Client.duplicate({self.client.space_id}) "
                        f"and pass in your Hugging Face token."
                    ) from None
                elif "error" in result:
                    raise ValueError(result["error"]) from None
                raise KeyError(
                    f"Could not find 'data' key in response. Response received: {result}"
                ) from ke
            return tuple(output)

        return _predict

    def insert_empty_state(self, *data) -> tuple:
        data = list(data)
        for i, input_component_type in enumerate(self.input_component_types):
            if input_component_type.is_state:
                data.insert(i, None)
        return tuple(data)

    def process_input_files(self, *data) -> tuple:
        data_ = []
        for i, d in enumerate(data):
            d = utils.traverse(
                d,
                partial(self._upload_file, data_index=i),
                utils.is_file_obj_with_meta,
            )
            data_.append(d)
        return tuple(data_)

    def process_predictions(self, *predictions):
        # If self.download_file is True, we assume that that the user is using the Client directly (as opposed
        # within gr.load) and therefore, download any files generated by the server and skip values for
        # components that the user likely does not want to see (e.g. gr.State, gr.Tab).
        if self.client.download_files:
            predictions = self.download_files(*predictions)
        if self.client._skip_components:
            predictions = self.remove_skipped_components(*predictions)
        predictions = self.reduce_singleton_output(*predictions)
        return predictions

    def download_files(self, *data) -> tuple:
        data_ = list(data)
        if self.client.protocol == "sse_v2.1":
            data_ = utils.traverse(
                data_, self._download_file, utils.is_file_obj_with_meta
            )
        else:
            data_ = utils.traverse(data_, self._download_file, utils.is_file_obj)
        return tuple(data_)

    def remove_skipped_components(self, *data) -> tuple:
        """"""
        data = [
            d
            for d, oct in zip(data, self.output_component_types, strict=False)
            if not oct.skip
        ]
        return tuple(data)

    def reduce_singleton_output(self, *data) -> Any:
        if self.client._skip_components:
            effective_output_components = [
                o for o in self.output_component_types if not o.skip
            ]
        else:
            effective_output_components = self.output_component_types
        if len(effective_output_components) == 1:
            return data[0]
        else:
            return data

    def _upload_file(self, f: dict, data_index: int) -> dict[str, str]:
        file_path = f["path"]
        orig_name = Path(file_path)
        if not utils.is_http_url_like(file_path):
            component_id = self.dependency["inputs"][data_index]
            component_config = next(
                (
                    c
                    for c in self.client.config["components"]
                    if c["id"] == component_id
                ),
                {},
            )
            max_file_size = self.client.config.get("max_file_size", None)
            max_file_size = math.inf if max_file_size is None else max_file_size
            if os.path.getsize(file_path) > max_file_size:
                raise ValueError(
                    f"File {file_path} exceeds the maximum file size of {max_file_size} bytes "
                    f"set in {component_config.get('label', '') + ''} component."
                )
            with open(file_path, "rb") as f_:
                files = [("files", (orig_name.name, f_))]
                r = httpx.post(
                    self.client.upload_url,
                    headers=self.client.headers,
                    cookies=self.client.cookies,
                    verify=self.client.ssl_verify,
                    files=files,
                    **self.client.httpx_kwargs,
                )
            r.raise_for_status()
            result = r.json()
            file_path = result[0]
        # Only return orig_name if has a suffix because components
        # use the suffix of the original name to determine format to save it to in cache.
        return {
            "path": file_path,
            "orig_name": utils.strip_invalid_filename_characters(orig_name.name),
            "meta": {"_type": "gradio.FileData"},
        }

    def _download_file(self, x: dict) -> str:
        url_path = self.root_url + "file=" + x["path"]
        if self.client.output_dir is not None:
            os.makedirs(self.client.output_dir, exist_ok=True)

        sha = hashlib.sha256()
        temp_dir = Path(tempfile.gettempdir()) / secrets.token_hex(20)
        temp_dir.mkdir(exist_ok=True, parents=True)

        with httpx.stream(
            "GET",
            url_path,
            headers=self.client.headers,
            cookies=self.client.cookies,
            verify=self.client.ssl_verify,
            follow_redirects=True,
            **self.client.httpx_kwargs,
        ) as response:
            response.raise_for_status()
            with open(temp_dir / Path(url_path).name, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=128 * sha.block_size):
                    sha.update(chunk)
                    f.write(chunk)

        directory = Path(self.client.output_dir) / sha.hexdigest()
        directory.mkdir(exist_ok=True, parents=True)
        dest = directory / Path(url_path).name
        shutil.move(temp_dir / Path(url_path).name, dest)
        return str(dest.resolve())

    def _sse_fn_v0(self, data: dict, hash_data: dict, helper: Communicator):
        with httpx.Client(
            timeout=httpx.Timeout(timeout=None),
            verify=self.client.ssl_verify,
            **self.client.httpx_kwargs,
        ) as client:
            return utils.get_pred_from_sse_v0(
                client,
                data,
                hash_data,
                helper,
                self.client.sse_url,
                self.client.sse_data_url,
                self.client.headers,
                self.client.cookies,
                self.client.ssl_verify,
                self.client.executor,
            )

    def _sse_fn_v1plus(
        self,
        helper: Communicator,
        event_id: str,
        protocol: Literal["sse_v1", "sse_v2", "sse_v2.1", "sse_v3"],
    ):
        return utils.get_pred_from_sse_v1plus(
            helper,
            self.client.headers,
            self.client.cookies,
            self.client.pending_messages_per_event,
            event_id,
            protocol,
            self.client.ssl_verify,
            self.client.executor,
        )


@document("result", "outputs", "status")
class Job(Future):
    """
    A Job is a wrapper over the Future class that represents a prediction call that has been
    submitted by the Gradio client. This class is not meant to be instantiated directly, but rather
    is created by the Client.submit() method.

    A Job object includes methods to get the status of the prediction call, as well to get the outputs
    of the prediction call. Job objects are also iterable, and can be used in a loop to get the outputs
    of prediction calls as they become available for generator endpoints.
    """

    def __init__(
        self,
        future: Future,
        communicator: Communicator | None = None,
        verbose: bool = True,
        space_id: str | None = None,
        _cancel_fn: Callable[[], None] | None = None,
    ):
        """
        Parameters:
            future: The future object that represents the prediction call, created by the Client.submit() method
            communicator: The communicator object that is used to communicate between the client and the background thread running the job
            verbose: Whether to print any status-related messages to the console
            space_id: The space ID corresponding to the Client object that created this Job object
        """
        self.future = future
        self.communicator = communicator
        self._counter = 0
        self.verbose = verbose
        self.space_id = space_id
        self.cancel_fn = _cancel_fn

    def __iter__(self) -> Job:
        return self

    def __next__(self) -> tuple | Any:
        if not self.communicator:
            raise StopIteration()

        while True:
            with self.communicator.lock:
                if len(self.communicator.job.outputs) >= self._counter + 1:
                    o = self.communicator.job.outputs[self._counter]
                    self._counter += 1
                    return o
                if (
                    self.communicator.job.latest_status.code == Status.FINISHED
                    and self._counter >= len(self.communicator.job.outputs)
                ):
                    raise StopIteration()
                time.sleep(0.001)

    async def __aiter__(self) -> AsyncGenerator[Update, None]:
        """Async iterator that yields all updates from the communicator.updates queue."""
        if not self.communicator:
            return

        while True:
            get = self.communicator.updates.get()
            try:
                update = await asyncio.wait_for(get, timeout=0.5)
                yield update
            except asyncio.TimeoutError:
                if self.done():
                    return
                continue

    def result(self, timeout: float | None = None) -> Any:
        """
        Return the result of the call that the future represents. Raises CancelledError: If the future was cancelled, TimeoutError: If the future didn't finish executing before the given timeout, and Exception: If the call raised then that exception will be raised.

        Parameters:
            timeout: The number of seconds to wait for the result if the future isn't done. If None, then there is no limit on the wait time.
        Returns:
            The result of the call that the future represents. For generator functions, it will return the final iteration.
        Example:
            from gradio_client import Client
            calculator = Client(src="gradio/calculator")
            job = calculator.submit("foo", "add", 4, fn_index=0)
            job.result(timeout=5)
            >> 9
        """
        return super().result(timeout=timeout)

    def outputs(self) -> list[tuple | Any]:
        """
        Returns a list containing the latest outputs from the Job.

        If the endpoint has multiple output components, the list will contain
        a tuple of results. Otherwise, it will contain the results without storing them
        in tuples.

        For endpoints that are queued, this list will contain the final job output even
        if that endpoint does not use a generator function.

        Example:
            from gradio_client import Client
            client = Client(src="gradio/count_generator")
            job = client.submit(3, api_name="/count")
            while not job.done():
                time.sleep(0.1)
            job.outputs()
            >> ['0', '1', '2']
        """
        if not self.communicator:
            return []
        else:
            with self.communicator.lock:
                return self.communicator.job.outputs

    def status(self) -> StatusUpdate:
        """
        Returns the latest status update from the Job in the form of a StatusUpdate
        object, which contains the following fields: code, rank, queue_size, success, time, eta, and progress_data.

        progress_data is a list of updates emitted by the gr.Progress() tracker of the event handler. Each element
        of the list has the following fields: index, length, unit, progress, desc. If the event handler does not have
        a gr.Progress() tracker, the progress_data field will be None.

        Example:
            from gradio_client import Client
            client = Client(src="gradio/calculator")
            job = client.submit(5, "add", 4, api_name="/predict")
            job.status()
            >> <Status.STARTING: 'STARTING'>
            job.status().eta
            >> 43.241  # seconds
        """
        time = datetime.now()
        cancelled = False
        if self.communicator:
            with self.communicator.lock:
                cancelled = self.communicator.should_cancel
        if cancelled:
            return StatusUpdate(
                code=Status.CANCELLED,
                rank=0,
                queue_size=None,
                success=False,
                time=time,
                eta=None,
                progress_data=None,
            )
        if self.done():
            if not self.future._exception:  # type: ignore
                return StatusUpdate(
                    code=Status.FINISHED,
                    rank=0,
                    queue_size=None,
                    success=True,
                    time=time,
                    eta=None,
                    progress_data=None,
                )
            else:
                return StatusUpdate(
                    code=Status.FINISHED,
                    rank=0,
                    queue_size=None,
                    success=False,
                    time=time,
                    eta=None,
                    progress_data=None,
                )
        elif not self.communicator:
            return StatusUpdate(
                code=Status.PROCESSING,
                rank=0,
                queue_size=None,
                success=None,
                time=time,
                eta=None,
                progress_data=None,
            )
        else:
            with self.communicator.lock:
                eta = self.communicator.job.latest_status.eta
                if self.verbose and self.space_id and eta and eta > 30:
                    print(
                        f"Due to heavy traffic on this app, the prediction will take approximately {int(eta)} seconds."
                        f"For faster predictions without waiting in queue, you may duplicate the space using: Client.duplicate({self.space_id})"
                    )
                return self.communicator.job.latest_status

    def cancel(self) -> bool:
        """Cancels the job as best as possible.

        If the app you are connecting to has the gradio queue enabled, the job
        will be cancelled locally as soon as possible. For apps that do not use the
        queue, the job cannot be cancelled if it's been sent to the local executor
        (for the time being).

        Note: In general, this DOES not stop the process from running in the upstream server
        except for the following situations:

        1. If the job is queued upstream, it will be removed from the queue and the server will not run the job
        2. If the job has iterative outputs, the job will finish as soon as the current iteration finishes running
        3. If the job has not been picked up by the queue yet, the queue will not pick up the job
        """
        if self.communicator:
            with self.communicator.lock:
                self.communicator.should_cancel = True
                if self.cancel_fn:
                    self.cancel_fn()
                return True
        return self.future.cancel()

    def __getattr__(self, name):
        """Forwards any properties to the Future class."""
        return getattr(self.future, name)
