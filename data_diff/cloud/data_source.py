import enum
import time
from typing import List, Optional

import pydantic
import rich
from rich.table import Table
from rich.prompt import Confirm, Prompt, FloatPrompt, IntPrompt, InvalidResponse

from .datafold_api import (
    DatafoldAPI, TCloudApiDataSourceConfigSchema, TCloudApiDataSource, TCloudApiDataSourceTestResult, TDsConfig,
    TestDataSourceStatus
)


DATA_SOURCE_TYPES_REQUIRED_SETTINGS = {
    'bigquery': {'projectId', 'jsonKeyFile', 'location'},
    'databricks': {'host', 'http_password', 'database', 'http_path'},
    'mysql': {'host', 'user', 'passwd', 'db'},
    'pg': {'host', 'user', 'port', 'password', 'dbname'},
    'postgres_aurora': {'host', 'user', 'port', 'password', 'dbname'},
    'postgres_aws_rds': {'host', 'user', 'port', 'password', 'dbname'},
    'redshift': {'host', 'user', 'port', 'password', 'dbname'},
    'snowflake': {'account', 'user', 'password', 'warehouse', 'role', 'default_db'},
}


class TDataSourceTestStage(pydantic.BaseModel):
    name: str
    status: TestDataSourceStatus
    description: str = ''


class TemporarySchemaPrompt(Prompt):
    response_type = str
    validate_error_message = "[prompt.invalid]Please enter Y or N"

    def process_response(self, value: str) -> str:
        """Convert choices to a bool."""

        if len(value.split('.')) != 2:
            raise InvalidResponse('Temporary schema should has a format <database>.<schema>')
        return value


def _validate_temp_schema(temp_schema: str):
    if len(temp_schema.split('.')) != 2:
        raise ValueError('Temporary schema should has a format <database>.<schema>')


def create_ds_config(ds_config: TCloudApiDataSourceConfigSchema, data_source_name: str) -> TDsConfig:
    options = _parse_ds_credentials(ds_config=ds_config, only_basic_settings=True)

    temp_schema = TemporarySchemaPrompt.ask('Temporary schema (<database>.<schema>)')
    float_tolerance = FloatPrompt.ask('Float tolerance', default=0.000001)

    return TDsConfig(
        name=data_source_name,
        type=ds_config.db_type,
        temp_schema=temp_schema,
        float_tolerance=float_tolerance,
        options=options,
    )


def _parse_ds_credentials(ds_config: TCloudApiDataSourceConfigSchema, only_basic_settings: bool = True):
    ds_options = {}
    basic_required_fields = DATA_SOURCE_TYPES_REQUIRED_SETTINGS.get(ds_config.db_type)
    for param_name, param_data in ds_config.config_schema.properties.items():
        if only_basic_settings and param_name not in basic_required_fields:
            continue

        title = param_data['title']
        default_value = param_data.get('default')
        is_password = bool(param_data.get('format'))

        type_ = param_data['type']
        if type_ == 'integer':
            value = IntPrompt.ask(title, default=default_value if default_value is not None else None)
        elif type_ == 'boolean':
            value = Confirm.ask(title)
        else:
            value = Prompt.ask(
                title,
                default=default_value if default_value is not None else None,
                password=is_password,
            )

        ds_options[param_name] = value
    return ds_options


def _check_data_source_exists(
    data_sources: List[TCloudApiDataSource],
    data_source_name: str,
) -> Optional[TCloudApiDataSource]:
    for ds in data_sources:
        if ds.name == data_source_name:
            return ds
    return None


def _test_data_source(api: DatafoldAPI, data_source_id: int, timeout: int = 64) -> List[TDataSourceTestStage]:
    job_id = api.test_data_source(data_source_id)

    checked_tests = {'connection', 'temp_schema', 'schema_download'}
    seconds = 1
    start = time.monotonic()
    results = []
    while True:
        tests = api.check_data_source_test_results(job_id)
        for test in tests:
            if test.name not in checked_tests:
                continue

            if test.status == 'done':
                checked_tests.remove(test.name)
                results.append(
                    TDataSourceTestStage(name=test.name, status=test.result.status, description=test.result.message)
                )

        if not checked_tests:
            break

        if time.monotonic() - start > timeout:
            for test_name in checked_tests:
                results.append(
                    TDataSourceTestStage(
                        name=test_name,
                        status=TestDataSourceStatus.SKIP,
                        description=f'Does not complete in {timeout} seconds',
                    )
                )
            break
        time.sleep(seconds)
        seconds *= 2

    return results


def _render_data_source(data_source: TCloudApiDataSource, title: str = '') -> None:
    table = Table(title=title, min_width=80)
    table.add_column("Parameter", justify="center", style="cyan")
    table.add_column("Value", justify="center", style="magenta")
    table.add_row("ID", str(data_source.id))
    table.add_row("Name", data_source.name)
    table.add_row("Type", data_source.type)
    rich.print(table)


def _render_available_data_sources(data_source_schema_configs: List[TCloudApiDataSourceConfigSchema]) -> None:
    config_names = [ds_config.name for ds_config in data_source_schema_configs]

    table = Table()
    table.add_column("", justify="center", style="cyan")
    table.add_column("Available data sources", style="magenta")
    for i, db_type in enumerate(config_names, start=1):
        table.add_row(str(i), db_type)
    rich.print(table)


def _render_data_source_test_results(test_results: List[TDataSourceTestStage]) -> None:
    table = Table(title='Test results', min_width=80)
    table.add_column("Test", justify="center", style="cyan", )
    table.add_column("Status", justify="center", style="magenta")
    table.add_column("Description", justify="center", style="magenta")
    for result in test_results:
        table.add_row(result.name, result.status, result.description)
    rich.print(table)


def get_or_create_data_source(api: DatafoldAPI) -> int:
    ds_configs = api.get_data_source_schema_config()
    data_sources = api.get_data_sources()

    _render_available_data_sources(data_source_schema_configs=ds_configs)
    db_type_num = IntPrompt.ask(
        'What data source type you want to create? Please, select a number',
        choices=list(map(str, range(1, len(ds_configs) + 1))),
        show_choices=False
    )

    ds_config = ds_configs[db_type_num - 1]
    default_ds_name = ds_config.name
    ds_name = Prompt.ask("Data source name", default=default_ds_name)

    ds = _check_data_source_exists(data_sources=data_sources, data_source_name=ds_name)
    if ds is not None:
        _render_data_source(data_source=ds, title=f'Found existing data source for name "{ds.name}"')
        use_existing_ds = Confirm.ask("Would you like to continue with the existing data source?")
        if not use_existing_ds:
            return get_or_create_data_source(api)
        return ds.id

    ds_config = create_ds_config(ds_config, ds_name)
    ds = api.create_data_source(ds_config)
    data_source_url = f'{api.host}/settings/integrations/dwh/{ds.type}/{ds.id}'
    _render_data_source(data_source=ds, title=f"Create a new data source with ID = {ds.id} ({data_source_url})")

    rich.print(
        'We recommend to run tests for a new data source. '
        'It requires some time but makes sure that the data source is configured correctly.'
    )
    run_tests = Confirm.ask('Would you like to run tests?')
    if run_tests:
        test_results = _test_data_source(api=api, data_source_id=ds.id)
        _render_data_source_test_results(test_results=test_results)
        if any(result.status == TestDataSourceStatus.FAILED for result in test_results):
            raise ValueError('Data source tests failed')

    return ds.id
