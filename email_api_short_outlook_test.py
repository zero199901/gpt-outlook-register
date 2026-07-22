#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import requests

from config import Config
from auth_flow import AuthFlow


class EmailApiClient:
    def __init__(self, base_url: str, api_key: str, *, verify_tls: bool = True):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.verify_tls = verify_tls
        self.session = requests.Session()
        self.log = logging.getLogger('email_api')

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        params['apikey'] = self.api_key
        url = f'{self.base_url}{path}'
        r = self.session.get(url, params=params, timeout=25, verify=self.verify_tls)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f'接口返回不是 JSON: http={r.status_code} body={(r.text or "")[:240]}') from e
        if data.get('code') != 0:
            raise RuntimeError(f'接口错误: {data}')
        return data

    def quota(self) -> dict[str, Any]:
        return self._get('/api/user/quota', {})

    def create_email(self, typ: str = 'short-outlook', service: str = '') -> dict[str, Any]:
        params = {'type': typ}
        if service:
            params['service'] = service
        data = self._get('/api/user/email', params)
        mail = data.get('data') or {}
        if not mail.get('email'):
            raise RuntimeError(f'领取邮箱返回缺少 data.email: {data}')
        return mail

    def latest_mail(self, email: str) -> dict[str, Any]:
        data = self._get('/api/user/mail', {'email': email})
        return data.get('data') or {}

    @staticmethod
    def extract_code(mail: dict[str, Any]) -> str:
        code = str(mail.get('code') or '').strip()
        m = re.search(r'\b\d{6}\b', code)
        if m:
            return m.group(0)
        for key in ('text', 'html', 'subject'):
            text = str(mail.get(key) or '')
            m = re.search(r'\b\d{6}\b', text)
            if m:
                return m.group(0)
        return ''

    def wait_code(self, email: str, timeout: int = 180, interval: int = 3, issued_after: Optional[float] = None) -> str:
        end = time.time() + timeout
        n = 0
        last_summary = ''
        while time.time() < end:
            n += 1
            try:
                mail = self.latest_mail(email)
                if mail:
                    code = self.extract_code(mail)
                    subj = str(mail.get('subject') or '')[:80]
                    sender = str(mail.get('from') or '')[:80]
                    last_summary = f'from={sender} subject={subj} code={code or "-"}'
                    self.log.info('[mail-api] 第 %s 次查信: %s', n, last_summary)
                    if code:
                        return code
                else:
                    self.log.info('[mail-api] 第 %s 次查信: 暂无邮件', n)
            except Exception as e:
                self.log.warning('[mail-api] 第 %s 次查信异常: %s', n, e)
            time.sleep(interval)
        raise TimeoutError(f'等待邮箱验证码超时 ({timeout}s)，最后状态: {last_summary or "暂无邮件"}')


class EmailApiMailProvider:
    last_persona = None
    outlook_exhausted = False

    def __init__(self, client: EmailApiClient, typ: str = 'short-outlook', service: str = ''):
        self.client = client
        self.typ = typ
        self.service = service
        self.email = ''
        self.catch_all_domain = ''
        self.account_info: dict[str, Any] = {}

    def create_mailbox(self) -> str:
        info = self.client.create_email(self.typ, self.service)
        self.account_info = info
        self.email = str(info['email']).strip()
        self.catch_all_domain = self.email.split('@', 1)[1] if '@' in self.email else ''
        logging.getLogger('email_api').info('[mail-api] 领取邮箱成功 type=%s email=%s', self.typ, self.email)
        return self.email

    def wait_for_otp(self, email_addr: str, timeout: int = 180, issued_after: Optional[float] = None) -> str:
        timeout = max(int(timeout or 180), 90)
        logging.getLogger('email_api').info('[mail-api] 开始轮询验证码 email=%s timeout=%ss', email_addr, timeout)
        return self.client.wait_code(email_addr, timeout=timeout, interval=3, issued_after=issued_after)

    def mark_outlook_dead(self, reason: str = '') -> None:
        logging.getLogger('email_api').warning('[mail-api] mark dead: %s', reason)
        self.outlook_exhausted = True


def main():
    ap = argparse.ArgumentParser(description='使用邮箱 API 的短效 Outlook 邮箱测试注册/取码')
    ap.add_argument('--base', default=os.getenv('EMAIL_API_BASE', 'https://mail.no-replyca.xyz'))
    ap.add_argument('--api-key', default=os.getenv('EMAIL_API_KEY', ''))
    ap.add_argument('--type', default='short-outlook')
    ap.add_argument('--service', default='')
    ap.add_argument('--proxy', default=os.getenv('PROXY', ''))
    ap.add_argument('--otp-timeout', type=int, default=int(os.getenv('OTP_TIMEOUT', '240')))
    ap.add_argument('--out', default='')
    ap.add_argument('--insecure', action='store_true')
    ap.add_argument('--sub2api-url', default=os.getenv('SUB2API_URL', ''))
    ap.add_argument('--sub2api-key', default=os.getenv('SUB2API_KEY', ''))
    ap.add_argument('--sub2api-group-ids', default=os.getenv('SUB2API_GROUP_IDS', '2'))
    ap.add_argument('--sub2api-timeout', type=int, default=int(os.getenv('SUB2API_TIMEOUT', '30')))
    ap.add_argument('--no-sub2api-upload', action='store_true')
    args = ap.parse_args()

    if not args.api_key:
        raise SystemExit('请设置 EMAIL_API_KEY 或传 --api-key')

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    os.environ['OTP_TIMEOUT'] = str(args.otp_timeout)
    os.environ.setdefault('WEBUI_ALLOW_LOGIN', '1')

    client = EmailApiClient(args.base, args.api_key, verify_tls=not args.insecure)
    q = client.quota().get('data') or {}
    logging.getLogger('email_api').info('[mail-api] 余额: left=%s total=%s used=%s', q.get('quota_left'), q.get('quota_total'), q.get('quota_used'))

    provider = EmailApiMailProvider(client, typ=args.type, service=args.service)
    cfg = Config(proxy=args.proxy.strip() or None)
    flow = AuthFlow(cfg)
    result = flow.run_register(provider)
    data = result.to_dict()
    data['_email_api_account'] = provider.account_info

    out = Path(args.out or f"email_api_result_{(provider.email or 'unknown').split('@', 1)[0]}.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n✅ 测试完成，结果已保存:', out.resolve(), flush=True)

    if args.sub2api_url and args.sub2api_key and not args.no_sub2api_upload:
        from webui import exporter

        sub2api_cfg = {
            'enabled': True,
            'sub2api_url': args.sub2api_url,
            'sub2api_api_key': args.sub2api_key,
            'sub2api_group_ids': args.sub2api_group_ids,
            'sub2api_timeout': str(args.sub2api_timeout),
        }

        def _upload_log(msg: str, level: str = 'info') -> None:
            logging.getLogger('sub2api_upload').info('%s', msg)

        upload_result = exporter.run_exports(data, sub2api_cfg=sub2api_cfg, log_fn=_upload_log).get('sub2api')
        data['_sub2api_upload'] = upload_result
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        if upload_result and upload_result.get('ok'):
            print('✅ SUB2API 上传成功:', upload_result.get('message') or upload_result, flush=True)
        else:
            print('⚠️ SUB2API 上传结果:', upload_result, flush=True)

    redacted = {k: ('***' if k.endswith('token') or k in ('cookie_header', 'agent_private_key') else v) for k, v in data.items()}
    if isinstance(redacted.get('_sub2api_upload'), dict):
        redacted['_sub2api_upload'] = dict(redacted['_sub2api_upload'])
    print(json.dumps(redacted, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
