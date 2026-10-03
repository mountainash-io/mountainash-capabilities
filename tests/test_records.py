import json

import pytest


def test_native_environment_normalization_preserves_unknown_and_label():
    from mountainash.core.capabilities.capture import Environment, EnvironmentCoordinate
    from mountainash_capabilities.records import serialize_environment

    env = Environment((EnvironmentCoordinate("package", "ibis", None),))
    assert serialize_environment(env) == {
        "coordinates": [
            {"kind": "package", "name": "ibis-framework", "version": None, "original_label": "ibis"}
        ]
    }


def test_strict_json_rejects_nonfinite_and_non_string_keys():
    from mountainash_capabilities.records import encode_record

    for data in ({"value": float("nan")}, {1: "ambiguous key"}):
        with pytest.raises(ValueError):
            encode_record(data)


def test_json_encoding_preserves_null_and_unicode():
    from mountainash_capabilities.records import encode_record

    value = {"label": "α", "version": None}
    assert json.loads(encode_record(value)) == value
