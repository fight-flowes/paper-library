"""Bounded connection probes. These checks never open or parse a PDF."""
from dataclasses import replace
import json
import shlex
import subprocess
import tempfile
import time

import httpx

from paper_library.preferences import PreferenceError


def test_model(settings):
    config = replace(settings.translation(), timeout=20)
    headers = {'Authorization': 'Bearer ' + config.api_key} if config.api_key else {}
    body = {'model': config.model, 'messages': [{'role': 'user', 'content': 'Reply exactly with OK. No other text.'}],
            'temperature': config.temperature, 'max_tokens': config.max_tokens, 'stream': False}
    if config.request_style == 'qwen':
        body['chat_template_kwargs'] = {'enable_thinking': False}
    elif config.request_style == 'deepseek':
        body['thinking'] = {'type': 'disabled'}
    started = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(20, connect=5), trust_env=False, follow_redirects=False) as client:
            response = client.post(config.base_url + '/chat/completions', headers=headers, json=body)
        if response.status_code in (401, 403):
            raise PreferenceError('模型认证失败，请检查接口地址和密钥')
        if not response.is_success:
            raise PreferenceError(f'模型调用失败（HTTP {response.status_code}），请检查模型名称和接口格式')
        content = response.json()['choices'][0]['message']['content']
        if not isinstance(content, str) or not content.strip():
            raise ValueError()
    except PreferenceError:
        raise
    except httpx.TimeoutException:
        raise PreferenceError('模型连接超时，请检查服务或端口转发') from None
    except httpx.HTTPError:
        raise PreferenceError('模型接口无法连接，请检查地址、服务和端口转发') from None
    except (ValueError, KeyError, TypeError, IndexError):
        raise PreferenceError('接口返回了无法使用的模型响应，请检查接口格式和模型名称') from None
    return {'message': '模型调用成功', 'model': config.model, 'seconds': round(time.monotonic() - started, 2)}


def test_remote(settings):
    from paper_library.ingest.remote import Transport, identity_arguments
    # Verify the machine before importing the deployed worker. The worker also
    # enforces its deployment machine-id before checking CUDA and model files.
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='paper-settings-check-') as temporary:
            transport = Transport(settings.values, temporary)
            arguments = identity_arguments(settings.values, check_worker=True)
            # Never validate new credentials through an old authenticated master.
            process = subprocess.run([transport.ssh, '-o', 'ControlMaster=no', '-o', 'ControlPath=none', *transport.options, '-p', transport.port,
                                      transport.target, shlex.join(arguments)],
                                     env=transport.environment, stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=30)
            if process.returncode:
                raise PreferenceError('cicore01 检查失败：请检查 SSH 认证、主机密钥、远程环境和 GPU；不会回退本机解析')
            result = json.loads(process.stdout)
            if not isinstance(result, dict) or result.get('cuda') is not True or not isinstance(result.get('gpu'), str):
                raise ValueError()
    except PreferenceError:
        raise
    except subprocess.TimeoutExpired:
        raise PreferenceError('cicore01 连接或环境检查超时；不会回退本机解析') from None
    except (OSError, ValueError, TypeError):
        raise PreferenceError('无法确认 cicore01 解析环境；请检查连接和运行路径') from None
    return {'message': 'cicore01 解析环境可用', 'gpu': result['gpu'][:160],
            'mineru_version': str(result.get('mineru_version', ''))[:40],
            'seconds': round(time.monotonic() - started, 2)}
