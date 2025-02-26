import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union, Optional, Dict, Any

import requests
from requests import ReadTimeout, Timeout
from requests.adapters import HTTPAdapter

from ibind import var
from ibind.support.errors import ExternalBrokerError
from ibind.support.logs import new_daily_rotating_file_handler, project_logger
from ibind.support.py_utils import filter_none, UNDEFINED

_LOGGER = project_logger(__file__)


@dataclass
class Result():
    """
    A class to encapsulate the result of an API request.

    This class is used to store and handle data returned from an API call. It includes the response data and
    the original request details.

    Attributes:
        data (Optional[Union[list, dict]]): The data returned from the operation. Can be either a list or a dictionary.
        request (Optional[dict]): Details of the request that resulted in this data.

    """
    data: Optional[Union[list, dict]] = field(default=None)
    request: Optional[dict] = field(default_factory=dict)

    def copy(self, data: Optional[Union[list, dict]] = UNDEFINED, request: Optional[dict] = UNDEFINED) -> 'Result':
        """
        Creates a copy of the current Result instance with optional modifications to its data or request.

        Parameters:
            data (Optional[Union[list, dict]], optional): The new data to be set in the copied Result.
                If 'UNDEFINED',the original data is retained. Defaults to UNDEFINED.
            request (Optional[dict], optional): The new request details to be set in the copied Result.
                If 'UNDEFINED', the original request is retained. Defaults to UNDEFINED.

        Returns:
            Result: A new Result instance with the specified modifications.
        """
        return Result(
            data=data if data is not UNDEFINED else self.data.copy(),
            request=request if request is not UNDEFINED else self.request.copy()
        )


def pass_result(data: dict, old_result: Result) -> Result:
    return old_result.copy(data=data)


class RestClient:
    """
    A base client class for interfacing with REST APIs.

    This class provides foundational methods to interact with REST APIs, such as sending HTTP requests
    (GET, POST, DELETE) and handling responses. It is designed to be extended by specific API client classes,
    providing them with common functionalities like request retries, response processing, and logging.

    Methods:
        get(path, params, log): Sends a GET request to the specified API endpoint.
        post(path, params, log): Sends a POST request to the specified API endpoint.
        delete(path, params, log): Sends a DELETE request to the specified API endpoint.
        request(method, endpoint, attempt, log, **kwargs): Sends an HTTP request to the API and handles retries and exceptions.

    Note:
        - This class is intended to be subclassed by specific API client implementations
          that can provide additional API-specific functionalities.
        - Logging is integrated into request methods, and each request is logged with the specified details.
    """

    def __init__(
            self,
            url: str,
            cacert: Union[os.PathLike, bool] = False,
            timeout: float = 10,
            max_retries: int = 3,
    ) -> None:
        """
        Parameters:
            url (str): The base URL for the REST API.
            cacert (Union[os.PathLike, bool], optional): Path to the CA certificate file for SSL verification,
                                                         or False to disable SSL verification. Defaults to False.
            timeout (float, optional): Timeout in seconds for the API requests. Defaults to 10.
            max_retries (int, optional): Maximum number of retries for failed API requests. Defaults to 3.
        """
        if url is None:
            raise ValueError(f"{self}: url must not be None")
        self.base_url = url
        if not url.endswith('/'):
            self.base_url += '/'

        self.cacert = cacert
        if not (isinstance(self.cacert, bool) or Path(self.cacert).exists()):
            raise ValueError(f"{self}: cacert must be a valid Path or Boolean")

        self._timeout = timeout
        self._max_retries = max_retries

        self._make_logger()

        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, pool_block=True)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _make_logger(self):
        self._logger = new_daily_rotating_file_handler('RestClient', os.path.join(var.LOGS_DIR, f'rest_client'))

    @property
    def logger(self):
        try:
            return self._logger
        except AttributeError:  # pragma: no cover
            self._make_logger()
            return self._logger

    def _get_headers(self, request_method: str, request_url: str):
        return {}

    def get(
            self,
            path: str,
            params: Optional[Dict[str, Any]] = None,
            base_url: str = None,
            extra_headers: dict = None,
            log: bool = True,
    ) -> Result:
        return self.request(method='GET', endpoint=path, base_url=base_url, extra_headers=extra_headers, params=params, log=log)

    def post(
            self,
            path: str,
            params: Optional[Dict[str, Any]] = None,
            base_url: str = None,
            extra_headers: dict = None,
            log: bool = True
    ) -> Result:
        return self.request(method='POST', endpoint=path, base_url=base_url, extra_headers=extra_headers, json=params, log=log)

    def delete(
            self,
            path: str,
            params: Optional[Dict[str, Any]] = None,
            base_url: str = None,
            extra_headers: dict = None,
            log: bool = True
    ) -> Result:
        return self.request('DELETE', path, log=log, base_url=base_url, extra_headers=extra_headers, json=params)

    def request(
            self,
            method: str,
            endpoint: str,
            base_url: str = None,
            extra_headers: dict = None,
            log: bool = True,
            **kwargs
            ) -> Result:
        """
        Sends an HTTP request to the specified endpoint using the given method, with retries on timeouts.

        This method constructs and sends an HTTP request to the REST API. It handles retries
        on read timeouts up to a maximum specified in '_max_retries'. The function logs each request and
        raises exceptions for other errors.

        Parameters:
            method (str): The HTTP method to use ('GET', 'POST', etc.).
            endpoint (str): The API endpoint to which the request is sent.
            base_url (str, optional): The base URL for the REST API. Defaults to the client's base URL.
            extra_headers (dict, optional): Additional headers to be included in the request. Defaults to None.
            log (bool, optional): Whether to log the request details. Defaults to True.
            **kwargs: Additional keyword arguments passed to the requests.request function.

        Returns:
            Result: A Result object containing the response from the API.

        Raises:
            TimeoutError: If the request times out and the maximum number of retries is reached.
            Exception: For any other errors that occur during the request.

        """
        return self._request(method, endpoint, base_url, extra_headers, log, **kwargs)

    def _request(
            self,
            method: str,
            endpoint: str,
            base_url: str = None,
            extra_headers: dict = None,
            log: bool = True,
            **kwargs
    ) -> Result:
        """
        Wrapper function which allows overriding the default request and error handling logic in the subclass.
        """

        base_url = base_url if base_url is not None else self.base_url

        endpoint = endpoint.lstrip("/")
        url = f"{base_url}{endpoint}"

        headers = self._get_headers(request_method=method, request_url=url)
        headers = {**headers, **(extra_headers or {})}

        # we want to allow default values used by IBKR, so we remove all None parameters
        kwargs = filter_none(kwargs)

        # we repeat the request attempts in case of ReadTimeouts up to max_retries
        for attempt in range(self._max_retries + 1):
            if log:
                self.logger.info(f'{method} {url} {kwargs}{" (attempt: " + str(attempt) + ")" if attempt > 0 else ""}')

            try:
                # response = requests.request(method, url, verify=self.cacert, headers=headers, timeout=self._timeout, **kwargs)
                response = self.session.request(method, url, verify=self.cacert, headers=headers, timeout=self._timeout, **kwargs)
                result = Result(request={'url': url, **kwargs})
                return self._process_response(response, result)

            except ReadTimeout as e:
                if attempt >= self._max_retries:
                    raise TimeoutError(f'{self}: Reached max retries ({self._max_retries}) for {method} {url} {kwargs}') from e

                _LOGGER.info(f'{self}: Timeout for {method} {url} {kwargs}, retrying attempt {attempt + 1}/{self._max_retries}')

                continue  # Continue to the next iteration for a retry
            except ExternalBrokerError:
                raise
            except Exception as e:
                self.logger.exception(e)
                raise ExternalBrokerError(f'{self}: request error: {str(e)}') from e

    def _process_response(self, response, result: Result) -> Result:
        try:
            response.raise_for_status()
            result.data = response.json()
            return result
        except Timeout as e:
            raise ExternalBrokerError(f'{self}: Timeout error ({self._timeout}S)', status_code=response.status_code) from e
        # TODO: add further network exceptions for graceful handling
        # TODO: add JSON parse exception handling
        except Exception as e:
            raise ExternalBrokerError(f'{self}: response error {result} :: {response.status_code} :: {response.reason} :: {response.text}', status_code=response.status_code) from e

    def __str__(self):
        return f'{self.__class__.__qualname__}'
