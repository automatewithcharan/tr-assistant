"""Python-startup bootstrap inherited by offline test subprocesses only."""

import os


if os.environ.get("PKSCREENER_TEST_OFFLINE", "") == "1":
    os.environ["LOKY_MAX_CPU_COUNT"] = "1"
    from pkscreener.offline_test_guard import is_secret_name
    for _name in tuple(os.environ):
        if is_secret_name(_name):
            os.environ.pop(_name, None)
    try:
        import dotenv
        dotenv.load_dotenv = lambda *_args, **_kwargs: False
        dotenv.dotenv_values = lambda *_args, **_kwargs: {}
        dotenv.find_dotenv = lambda *_args, **_kwargs: ""
    except Exception:
        pass
    from pkscreener.offline_test_guard import install
    install()
