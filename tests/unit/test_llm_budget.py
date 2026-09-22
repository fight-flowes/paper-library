import multiprocessing
from dataclasses import replace
import httpx
import pytest
from paper_library.config import TranslationSettings
from paper_library.llm.runtime import model_client


def occupy(config, ready, release):
    with model_client(config):
        ready.set()
        release.wait(10)


def test_model_limit_is_shared_between_processes_and_released_after_exit(tmp_path):
    config=TranslationSettings('http://model/v1','test','',timeout=.15,budget=1,budget_directory=tmp_path)
    ctx=multiprocessing.get_context('spawn');ready=ctx.Event();release=ctx.Event()
    process=ctx.Process(target=occupy,args=(config,ready,release));process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(httpx.PoolTimeout):
            with model_client(config):pass
        # A distinct endpoint does not consume this endpoint's allowance.
        with model_client(replace(config,base_url='http://other/v1')):pass
    finally:
        process.terminate();process.join(10)
    with model_client(config):pass
