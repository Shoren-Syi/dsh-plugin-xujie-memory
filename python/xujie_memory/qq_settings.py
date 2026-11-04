"""保存 QQ 连接配置；浏览器只读取令牌是否存在，不读取令牌本身。"""

from __future__ import annotations

import json
import re
import uuid
from urllib.parse import urlsplit

from .qq_budget import BudgetPolicy, QQBudget


def _url(value: str, protocols: tuple[str, ...]) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in protocols or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('连接地址格式不合法')
    return value.rstrip('/')


def _ids(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('QQ 名单必须为不超过一百项的列表')
    result = [str(item) for item in value]
    if not all(re.fullmatch(r'\d{1,20}', item) for item in result):
        raise ValueError('QQ 名单只能包含数字账号')
    return list(dict.fromkeys(result))


class QQSettings:
    def __init__(self, engine):
        self.engine = engine

    def get(self, scope: str) -> dict:
        row = self.engine._conn().execute('SELECT * FROM qq_connections WHERE scope_id=?', (scope,)).fetchone()
        settings = json.loads(row['settings_json']) if row else None
        if settings is not None: settings['enabled'] = bool(row['enabled'])
        return {'configured': bool(row), 'settings': settings,
                'has_token': bool(row['access_token']) if row else False,
                'budget': QQBudget(self.engine).status(scope)}

    def active(self) -> dict | None:
        row = self.engine._conn().execute('SELECT * FROM qq_connections WHERE enabled=1').fetchone()
        if row is None:
            return None
        return {**json.loads(row['settings_json']), 'accessToken': row['access_token'],
                'budgetScope': row['scope_id'], 'revision': row['revision']}

    def save(self, scope: str, raw: dict) -> None:
        if not scope or not isinstance(raw, dict):
            raise ValueError('QQ 配置不合法')
        enabled, mention = raw.get('enabled', False), raw.get('requireMention', True)
        if type(enabled) is not bool or type(mention) is not bool:
            raise ValueError('QQ 开关必须是布尔值')
        def field(name: str, limit: int = 300) -> str:
            value = raw.get(name, '')
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f'{name} 不合法')
            return value.strip()
        ws, http, self_id = field('wsUrl'), field('httpUrl'), field('selfId', 20)
        provider, model = field('provider', 100), field('model', 100)
        maximum = raw.get('maxTokens', 512)
        if type(maximum) is not int or not 1 <= maximum <= 65536:
            raise ValueError('输出 token 上限不合法')
        if ws: ws = _url(ws, ('ws', 'wss'))
        if http: http = _url(http, ('http', 'https'))
        if self_id and not re.fullmatch(r'\d{1,20}', self_id): raise ValueError('机器人 QQ 号不合法')
        private, group = _ids(raw.get('privateAllow', [])), _ids(raw.get('groupAllow', []))
        if enabled and (not all((ws, http, self_id, provider, model)) or not (private or group)):
            raise ValueError('启用前需要连接、账号、模型和允许的聊天对象')
        policy_raw = raw.get('policy')
        if policy_raw is not None and not isinstance(policy_raw, dict): raise ValueError('预算配置不合法')
        policy = BudgetPolicy(**policy_raw) if policy_raw is not None else None
        activity_limits = raw.get('activityLimits', {})
        if not isinstance(activity_limits, dict) or any(key not in ('chat', 'proactive', 'qzone') for key in activity_limits):
            raise ValueError('活动次数上限不合法')
        if any(type(value) is not int or value <= 0 for value in activity_limits.values()):
            raise ValueError('活动次数上限必须是正整数')
        schedule = raw.get('schedule', {})
        if not isinstance(schedule, dict):
            raise ValueError('主动任务配置不合法')
        def scheduled(name: str) -> dict:
            item = schedule.get(name, {})
            if not isinstance(item, dict) or type(item.get('enabled', False)) is not bool:
                raise ValueError('主动任务开关不合法')
            interval = item.get('intervalMinutes', 1440)
            prompt = item.get('prompt', '')
            if type(interval) is not int or not 30 <= interval <= 43200:
                raise ValueError('主动任务间隔须在 30 分钟至 30 天之间')
            if not isinstance(prompt, str) or len(prompt) > 1000:
                raise ValueError('主动任务提示词不合法')
            result = {'enabled': item.get('enabled', False), 'intervalMinutes': interval, 'prompt': prompt.strip()}
            if name == 'proactive':
                channel = item.get('channel', 'private')
                if channel not in ('private', 'group'):
                    raise ValueError('主动发言类型不合法')
                target = str(item.get('target', ''))
                targets = private if channel == 'private' else group
                if result['enabled'] and target not in targets:
                    raise ValueError('主动发言对象必须位于对应白名单中')
                result.update(channel=channel, target=target)
            else:
                visibility = item.get('visibility', 64)
                if type(visibility) is not int or visibility not in (1, 4, 64):
                    raise ValueError('空间动态可见范围不合法')
                result['visibility'] = visibility
            if result['enabled'] and not result['prompt']:
                raise ValueError('启用主动任务需要填写内容要求')
            if result['enabled'] and name not in activity_limits:
                raise ValueError('启用主动任务需要设置每日次数上限')
            return result
        schedules = {'proactive': scheduled('proactive'), 'qzone': scheduled('qzone')}
        token = raw.get('accessToken')
        if token is not None and (not isinstance(token, str) or len(token) > 512): raise ValueError('连接令牌不合法')
        settings = {'enabled': enabled, 'wsUrl': ws, 'httpUrl': http, 'selfId': self_id,
                    'privateAllow': private, 'groupAllow': group, 'requireMention': mention,
                    'provider': provider, 'model': model, 'maxTokens': maximum, 'schedule': schedules}
        with self.engine._tx() as conn:
            old = conn.execute('SELECT access_token FROM qq_connections WHERE scope_id=?', (scope,)).fetchone()
            if policy is not None: QQBudget(self.engine).configure(scope, policy)
            for activity, count in activity_limits.items():
                QQBudget(self.engine).configure_activity(scope, activity, count)
            if enabled and not QQBudget(self.engine).status(scope)['configured']:
                raise ValueError('启用前需要设置预算与模型价格')
            if enabled:
                conn.execute('UPDATE qq_connections SET enabled=0 WHERE scope_id!=? AND enabled=1', (scope,))
            saved_token = token if token is not None else (old['access_token'] if old else '')
            conn.execute('INSERT INTO qq_connections VALUES (?,?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET'
                         ' settings_json=excluded.settings_json,access_token=excluded.access_token,enabled=excluded.enabled,revision=excluded.revision',
                         (scope, json.dumps(settings, ensure_ascii=False), saved_token, int(enabled), uuid.uuid4().hex))
