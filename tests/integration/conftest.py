import pytest


@pytest.fixture
def coherent_path():
    """Path to unzipped coherent data - see http://hdx.mitre.org/downloads/coherent-08-10-2021.zip."""
    return 'coherent/'


@pytest.fixture
def output_path():
    """Path to output data."""
    return 'output/'


@pytest.fixture
def number_of_files_to_sample():
    """If set, verify N """
    return 100  # set to None to verify all
