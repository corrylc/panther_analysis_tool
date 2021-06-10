"""
Panther Analysis Tool is a command line interface for writing,
testing, and packaging policies/rules.
Copyright (C) 2020 Panther Labs Inc

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from dataclasses import dataclass
import fnmatch
import json
import logging
import os
from typing import List, Dict, Tuple, Optional, Any, cast

import boto3
from botocore import client
from ruamel.yaml import YAML
from ruamel.yaml.scanner import ScannerError
from ruamel.yaml.parser import ParserError


logger = logging.getLogger(__file__)


class Client:
    _LAMBDA_NAME = 'panther-logtypes-api'
    _LIST_SCHEMAS_ENDPOINT = 'ListSchemas'
    _PUT_SCHEMA_ENDPOINT = 'PutUserSchema'

    def __init__(self) -> None:
        self._lambda_client = None

    @property
    def lambda_client(self) -> client.BaseClient:
        if self._lambda_client is None:
            self._lambda_client = boto3.client("lambda")
        return self._lambda_client

    def list_schemas(self) -> Tuple[bool, dict]:
        """
        Retrieves the list of user-defined schemas.

        Returns:
            A boolean flag denoting if the request was successful,
            along with the response payload.
        """
        return self._invoke(
            self._create_lambda_request(
                endpoint=self._LIST_SCHEMAS_ENDPOINT,
                payload={
                    'isManaged': False
                }
            )
        )

    def put_schema(self, name: str, definition: str, revision: int,  # pylint: disable=too-many-arguments
                   description: str, reference_url: str) -> Tuple[bool, dict]:
        """
        Update a custom schema.

        Args:
            name: the schema name which uniquely identifies the schema
            definition: the YAML spec for this schema
            revision: the current revision number used to perform
                      backwards incompatibility checks
            description: an optional schema description
            reference_url: an optional schema reference URL

        Returns:
            A boolean flag denoting if the request was successful,
            along with the response payload.
        """
        return self._invoke(
            self._create_lambda_request(
                endpoint=self._PUT_SCHEMA_ENDPOINT,
                payload=dict(
                    name=name,
                    referenceURL=reference_url,
                    description=description,
                    spec=definition,
                    revision=revision,
                )
            )
        )

    def _invoke(self, request: dict) -> Tuple[bool, dict]:
        response = self.lambda_client.invoke(**request)
        response = json.loads(response["Payload"].read().decode("utf-8"))
        api_error = response.get("error")
        if api_error is not None:
            return False, response
        return True, response

    def _create_lambda_request(self, endpoint: str, payload: dict) -> dict:
        return dict(
            FunctionName=self._LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps({endpoint: payload})
        )


@dataclass
class UploaderResult:
    # The path of the schema definition file
    filename: str
    # The schema name / identifier, e.g. Custom.SampleSchema
    name: Optional[str]
    # The Lambda invocation response payload (PutUserSchema endpoint)
    api_response: Optional[Dict[str, Any]] = None
    # The schema specification in YAML form
    definition: Optional[Dict[str, Any]] = None
    # Any error encountered during processing will be stored here
    error: Optional[str] = None
    # Flag to signify whether the schema was created or updated
    existed: Optional[bool] = None


@dataclass
class ProcessedFile:
    # Any error message produced during YAML parsing
    error: Optional[str] = None
    # The raw file contents
    raw: str = ''
    # The deserialized schema
    yaml: Optional[Dict[str, Any]] = None


class Uploader:
    _SCHEMA_NAME_PREFIX = 'Custom.'
    _SCHEMA_FILE_GLOB_PATTERNS = ('*.yml', '*.yaml')

    def __init__(self, path: str):
        self._path = path
        self._files: Optional[List[str]] = None
        self._api_client: Optional[Client] = None
        self._existing_schemas: Optional[List[Dict[str, Any]]] = None

    @property
    def api_client(self) -> Client:
        if self._api_client is None:
            self._api_client = Client()
        return self._api_client

    @property
    def files(self) -> List[str]:
        """
        Resolves the list of schema definition files.
        Returns:
            A list of absolute paths to the schema files.
        """
        if self._files is None:
            self._files = discover_files(self._path, self._SCHEMA_FILE_GLOB_PATTERNS)
        return self._files

    @property
    def existing_schemas(self) -> List[Dict[str, Any]]:
        """
        Retrieves and caches in the instance state the list
        of available user-defined schemas.

        Returns:
             List of user-defined schema records.
        """
        if self._existing_schemas is None:
            success, response = self.api_client.list_schemas()
            if not success:
                raise RuntimeError('unable to retrieve custom schemas')
            self._existing_schemas = response['results']
        return self._existing_schemas

    def find_schema(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Find schema by name.

        Returns:
             The decoded YAML schema or None if no matching name is found.
        """
        for schema in self.existing_schemas:
            if schema['name'] == name:
                return schema
        return None

    def process(self) -> List[UploaderResult]:
        """
        Processes all potential schema files found in the given path.
        For updates it is required to retrieve description, revision number,
        and reference URL from the backend for each schema. More specifically:
        - Reference URL and description can be included in the definition, but are
          defined as additional metadata in the UI.
        - A matching revision number must be provided when making update requests,
          otherwise validation fails.

        Returns:
             A list of UploaderResult records that can be used
             for reporting the applied changes and errors.
        """
        if not self.files:
            logger.warning("No files found in path '%s'", self._path)
            return []

        processed_files = self._load_from_yaml(self.files)
        results = []
        # Add results for files that could not be loaded first
        for filename, processed_file in processed_files.items():
            if processed_file.error is not None:
                results.append(
                    UploaderResult(
                        name=None,
                        filename=filename,
                        error=processed_file.error,
                    )
                )

        for filename, processed_file in processed_files.items():
            # Skip any files with load errors, we have already included
            # them in the previous loop
            if processed_file.error is not None:
                continue

            logger.info("Processing file %s", filename)

            name, error = self._extract_schema_name(processed_file.yaml)
            result = UploaderResult(filename=filename, name=name, error=error)

            # Don't attempt to perform an update, if we could not extract the name from the file
            if not result.error:
                existed, success, response = self._update_or_create_schema(name, processed_file)
                result.existed = existed
                if not success:
                    api_error = response.get('error')
                    if api_error is not None:
                        result.error = f'failure to update schema {name}: ' \
                                       f'code={api_error["code"]}, message={api_error["message"]}'
                result.api_response = response
            results.append(result)
        return results

    @staticmethod
    def _load_from_yaml(files: List[str]) -> Dict[str, ProcessedFile]:
        yaml_parser = YAML(typ="safe")

        processed_files = {}
        for filename in files:
            logger.info('Loading schema from file %s', filename)
            processed_file = ProcessedFile()
            processed_files[filename] = processed_file
            try:
                with open(filename, "r") as schema_file:
                    processed_file.raw = schema_file.read()
                processed_file.yaml = yaml_parser.load(processed_file.raw)
            except (ParserError, ScannerError) as exc:
                processed_file.error = f"invalid YAML: {exc}"
        return processed_files

    def _extract_schema_name(
            self,
            definition: Optional[Dict[str, Any]]
    ) -> Tuple[str, Optional[str]]:
        if definition is None:
            raise ValueError('definition cannot be None')

        name = definition.get('schema')

        if name is None:
            return "", "key 'schema' not found"

        if not name.startswith(self._SCHEMA_NAME_PREFIX):
            return "", f"'schema' field: value must start" \
                       f" with the prefix '{self._SCHEMA_NAME_PREFIX}'"

        return name, None

    def _update_or_create_schema(
            self,
            name: str,
            processed_file: ProcessedFile
    ) -> Tuple[bool, bool, Dict[str, Any]]:
        existing_schema = self.find_schema(name)
        current_reference_url = ''
        current_description = ''
        current_revision = 0
        definition = cast(Dict[str, Any], processed_file.yaml)
        existed = False
        if existing_schema is not None:
            existing_schema = cast(Dict[str, Any], existing_schema)
            existed = True
            current_reference_url = existing_schema.get('referenceURL', '')
            current_description = existing_schema.get('description', '')
            current_revision = existing_schema['revision']
        reference_url = definition.get('referenceURL', current_reference_url)
        description = definition.get('description', current_description)
        logger.debug('updating schema %s at revision %d, using '
                     'referenceURL=%s, '
                     'description=%s',
                     name,
                     current_revision,
                     reference_url,
                     description)
        success, response = self.api_client.put_schema(
            name=name,
            definition=processed_file.raw,
            revision=current_revision,
            reference_url=reference_url,
            description=description,
        )
        return existed, success, response


def discover_files(base_path: str, patterns: Tuple[str, ...]) -> List[str]:
    """
    Recursively locates files that match the given glob patterns.

    Args:
         base_path: the base directory for recursively searching for files
         patterns: a list of glob patterns that the filenames should match

    Returns:
        A sorted list of absolute paths.
    """
    files = []
    for directory, _, filenames in os.walk(base_path):
        for filename in filenames:
            for pattern in patterns:
                if fnmatch.fnmatch(filename, pattern):
                    files.append(os.path.join(directory, filename))
    return sorted(files)


def normalize_path(path: str) -> Optional[str]:
    """Resolve the given path to its absolute form, taking into
    account user home prefix notation.
    Returns:
        The absolute path or None if the path does not exist.
    """
    absolute_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(absolute_path):
        return None
    return absolute_path


def report_summary(base_path: str, results: List[UploaderResult]) -> List[Tuple[bool, str]]:
    """
    Translate uploader results to descriptive status messages.

    Returns:
         A list of status messages along with the corresponding status flag for each message.
         Failure messages are flagged with True.
    """
    summary = []
    for result in sorted(results, key=lambda r: r.filename):
        filename = result.filename.split(base_path)[-1].strip(os.path.sep)
        if result.error:
            summary.append((True, f"Failed to update schema from definition"
                                  f" in file '{filename}': {result.error}"))
        else:
            if result.existed:
                operation = 'updated'
            else:
                operation = 'created'
            summary.append((False, f"Successfully {operation} schema '{result.name}' "
                                   f"from definition in file '{filename}'"))
    return summary