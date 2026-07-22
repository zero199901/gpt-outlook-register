#!/usr/bin/env python3
import argparse
import json
import logging
import re
from pathlib import Path

from config import Config
from auth_flow import AuthFlow


class ManualMailProvider:
    last_persona = None
    outlook_exhausted = False

    def __init__(self, email: str):
        self.email = email
        self.catch_all_domain = email.split('@', 1)[1] if '@' in email else ''

    def create_mailbox(self) -> str:
        logging.getLogger('manual').info('[mail] 使用手动接码邮箱: %s', self.email)
        return self.email

    def wait_for_otp(self, email_addr: str, timeout: int = 600, issued_after=None) -> str:
        print('\n' + '=' * 72, flush=True)
        print(f'已触发验证码发送，请手动查看邮箱并把 {email_addr} 收到的 6 位验证码发给我。', flush=True)
        print('在这里输入验证码后回车：', flush=True)
        print('=' * 72, flush=True)
        while True:
            code = input('OTP> ').strip()
            m = re.search(r'\b(\d{6})\b', code)
            if m:
                return m.group(1)
            print('请输入 6 位数字验证码。', flush=True)

    def mark_outlook_dead(self, reason: str = '') -> None:
        logging.getLogger('manual').warning('[mail] mark dead ignored: %s', reason)
        self.outlook_exhausted = True


def main():
    ap = argparse.ArgumentParser(description='手动邮箱 OTP 注册/登录测试')
    ap.add_argument('email')
    ap.add_argument('--proxy', default='')
    ap.add_argument('--out', default='')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    out = Path(args.out or f"manual_otp_result_{args.email.split('@', 1)[0]}.json")
    cfg = Config(proxy=args.proxy.strip() or None)
    flow = AuthFlow(cfg)
    result = flow.run_register(ManualMailProvider(args.email))
    data = result.to_dict()
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n✅ 手动接码测试完成，结果已保存:', out.resolve(), flush=True)
    redacted = {
        k: ('***' if k.endswith('token') or k in ('cookie_header', 'agent_private_key') else v)
        for k, v in data.items()
    }
    print(json.dumps(redacted, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
