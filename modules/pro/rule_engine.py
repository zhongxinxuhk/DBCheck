# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
DBCheck Pro 插件规则引擎
支持 YAML 规则描述，用户无需修改 Python 代码即可添加/禁用检查规则。

安全策略：
- 不使用 exec()，使用 eval() 并在白名单沙箱中执行
- 仅允许访问 params 和 context 中的变量
- ast 预检查：拒绝import、getattr、setattr 等危险操作
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck



import os
import ast
import re
import yaml
from typing import Dict, List, Any, Optional, Tuple


# ── 允许在 condition 中安全调用的值方法（仅字符串/常用不可变方法，
#    禁止任何危险属性/方法，如 __class__ / __bases__ / eval / exec 等）────────
_SAFE_METHODS = {
    'get',
    'upper', 'lower', 'strip', 'lstrip', 'rstrip',
    'replace', 'startswith', 'endswith', 'split', 'rsplit',
    'title', 'capitalize', 'count', 'find', 'index',
    'isdigit', 'isalpha', 'isnumeric', 'isalnum', 'isspace',
}


# ── 库型归一化 ─────────────────────────────────────────────────────────────
# 规则文件的 db_types 用规范短名（pg/dm/gbase...），而巡检引擎/入口可能传
# postgresql/dm8/gbase8a/sqlserver_jdbc 等别名。历史上 pg.yaml 声明 ['pg'] 而
# 调用方传 'postgresql'，导致 12 条规则从未执行——统一在此归一化，两边都映射到
# 同一个规范名后再比较。
_RULE_DB_ALIASES = {
    'postgresql': 'pg',
    'postgres': 'pg',
    'dm8': 'dm',
    'oracle_jdbc': 'oracle',
    'sqlserver_jdbc': 'sqlserver',
    'yasdb': 'yashandb',   # 崖山：入口常传 yasdb，规则文件声明 yashandb
}


def normalize_db_type(db_type):
    """把各入口的库型别名归一化为规则库使用的规范短名。"""
    t = str(db_type or '').strip().lower()
    return _RULE_DB_ALIASES.get(t, t)


def _int(v, default: int = 0) -> int:
    """规则引擎安全整型解析：容忍逗号/百分号/空白，解析失败回退 default。

    供 YAML condition 中以 _int(x) 形式调用（如 yasdb tablespace 规则）。
    """
    try:
        return int(str(v).replace(',', '').replace('%', '').strip())
    except (ValueError, TypeError):
        return default


# ── 危险操作黑名单（ast 预检查）──────────────────────
_DANGER_AST_NODES = (
    ast.Import, ast.ImportFrom,
    ast.Call,   # 禁止函数调用（如 __import__()，getattr() 等）
)
# 实际上我们需要允许一些安全函数调用（min, max, len 等）
# 所以不对 Call 节点做全局拒绝，而是在 _eval_expr 中做白名单


def _safe_eval(expr: str, context: Dict, params: Dict) -> Tuple[bool, str]:
    """
    在安全沙箱中执行表达式。
    仅允许：比较运算、算术运算、逻辑运算、取值操作、白名单 builtins，
           以及白名单安全方法调用（如 .upper()/.strip()/.startswith() 等）。
    禁止：import、dunder 属性（__x__）、白名单外的属性/方法/函数调用。

    Returns:
        (result: bool, error_msg: str)
    """
    # 构建白名单 locals
    locals_dict = {}
    locals_dict.update(params)

    # 允许的安全函数
    safe_builtins = {
        'min': min, 'max': max, 'len': len,
        'int': int, 'float': float, 'str': str,
        'abs': abs, 'round': round,
        'any': any, 'all': all,
        'sum': sum,
        '_int': _int,
    }

    try:
        # ast 预检查
        tree = ast.parse(expr, mode='eval')
        for node in ast.walk(tree):
            # 拒绝 import
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return False, "不允许 import 操作"
            # 属性访问：仅允许白名单安全方法，禁止 dunder / 未知属性
            if isinstance(node, ast.Attribute):
                attr = node.attr
                if attr.startswith('__') and attr.endswith('__'):
                    return False, f"不允许的属性访问: {attr}"
                if attr not in _SAFE_METHODS:
                    return False, f"不允许的属性访问: {attr}"
                continue  # 白名单方法（通常是 .upper()/.strip()）放行
            # 函数/方法调用
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    if func.id in safe_builtins:
                        continue
                    return False, f"不允许函数调用: {func.id}"
                if isinstance(func, ast.Attribute):
                    if func.attr in _SAFE_METHODS:
                        continue
                    return False, f"不允许的方法调用: {func.attr}"
                return False, f"不允许的函数调用: {ast.dump(func)}"

        # 注意：eval() 接收的是 code object，不能直接传 AST 节点
        # （eval(ast_node) 在 CPython 下会抛
        #  "eval() arg 1 must be a string, bytes or code object"）。
        # 必须先 compile() 成 code object 才能执行——这是规则引擎此前
        #  对所有 condition 都静默失败（"全库 YAML 未生效"）的真正根因之一。
        code = compile(tree, '<rule_condition>', 'eval')
        result = eval(code, {'__builtins__': safe_builtins}, locals_dict)
        return bool(result), ""
    except Exception as e:
        return False, str(e)


class _SafeContext:
    """递归安全包装：同时支持属性与下标访问，dict/list 自动递归包装。

    让 YAML 参数表达式里的 `context.x` / `context['x']` / `context.x[0].y`
    在 context 是普通 dict 时也能正确取值。
    同时委托 dict/list 的内置方法（如 .get()/.items()），使
    `context.get('key')` 调用底层真实方法而非被误当作 key 查找。
    """

    def __init__(self, data):
        self._d = data

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        if isinstance(self._d, dict):
            # 若 name 是 dict 内置方法（如 get/keys/values），委托给真实方法，
            # 否则按 key 查找（使 context.x == context['x']）
            if hasattr(type(self._d), name):
                attr = getattr(self._d, name)
                if callable(attr):
                    def _method(*args, **kwargs):
                        return _wrap(attr(*args, **kwargs))
                    return _method
                return _wrap(attr)
            return _wrap(self._d.get(name))
        return _wrap(getattr(self._d, name, None))

    def __getitem__(self, key):
        if isinstance(self._d, (dict, list)):
            try:
                return _wrap(self._d[key])
            except (KeyError, IndexError, TypeError):
                return None
        return None

    def __contains__(self, key):
        try:
            return key in self._d
        except TypeError:
            return False

    def __iter__(self):
        """显式提供迭代协议，避免旧式迭代协议造成死循环。

        本类只实现 __getitem__ 时，`for x in context.some_key` 会走 Python 的旧式
        迭代协议：不断请求 __getitem__(0)、__getitem__(1)……直到抛 IndexError 为止。
        而本类的 __getitem__ 越界时返回 None 而不是抛 IndexError，于是迭代永不终止
        ——YAML 规则里只要写一句列表推导（如 [p for p in context.params]）就会把
        巡检进程挂死。这里显式返回真实迭代器：dict 迭代 key、list 迭代元素，
        其余不可迭代（与 _wrap 只包装 dict/list 保持一致）。
        """
        if isinstance(self._d, (dict, list)):
            return iter(self._d)
        return iter(())

    def __len__(self):
        try:
            return len(self._d)
        except TypeError:
            return 0

    def __bool__(self):
        try:
            return bool(self._d)
        except TypeError:
            return False

    def __repr__(self):
        return repr(self._d)


def _wrap(v):
    if isinstance(v, (dict, list)):
        return _SafeContext(v)
    return v


def _unwrap(v):
    if isinstance(v, _SafeContext):
        return _unwrap(v._d)
    if isinstance(v, dict):
        return {k: _unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


# 参数表达式允许的安全 builtins（比条件更宽松，但仍是白名单）
# 注意 _int 已在文件上方定义为模块级函数，可直接引用
_PARAM_BUILTINS = {
    'int': int, 'float': float, 'str': str,
    'len': len, 'min': min, 'max': max, 'abs': abs, 'round': round,
    'any': any, 'all': all, 'sum': sum, 'bool': bool,
    '_int': _int,
}


def _safe_eval_param(expr: str, context: Dict) -> Any:
    """安全求值参数表达式（${context...} 内部内容）。

    允许：比较/算术/逻辑运算、条件表达式(IfExp)、下标、非 dunder 属性访问、
         白名单 builtins（int/len/_int/...）与 _SAFE_METHODS 方法调用。
    禁止：import、dunder 属性（__x__）、白名单外的函数/方法调用。
    返回解包后的原生值；任何失败返回 None（与 gbase 表达式内部的 else 兜底配合）。
    """
    try:
        tree = ast.parse(expr, mode='eval')
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return None
            if isinstance(node, ast.Attribute):
                if node.attr.startswith('__') and node.attr.endswith('__'):
                    return None
                continue
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    if func.id not in _PARAM_BUILTINS:
                        return None
                elif isinstance(func, ast.Attribute):
                    if func.attr not in _SAFE_METHODS:
                        return None
                else:
                    return None
        code = compile(tree, '<rule_param>', 'eval')
        result = eval(code, {'__builtins__': _PARAM_BUILTINS}, {'context': _SafeContext(context)})
        result = _unwrap(result)
        # 数值字符串转数值（SHOW VARIABLES 等返回的 Value 多为字符串，如 '200'/'95'）
        if isinstance(result, str):
            try:
                return int(result)
            except (ValueError, TypeError):
                pass
            try:
                return float(result)
            except (ValueError, TypeError):
                pass
        return result
    except Exception:
        return None


def _resolve_param(param_expr: str, context: Dict) -> Any:
    """
    解析参数表达式，支持：
    - 字面量：90, "hello", 1.5
    - 完整 Python 表达式（${context...}）：三元/方法调用/下标等，
      交由 _safe_eval_param 在白名单沙箱中安全求值
    """
    if not isinstance(param_expr, str):
        return param_expr
    m = re.match(r'^\$\{(.+)\}$', param_expr.strip())
    if m:
        return _safe_eval_param(m.group(1), context)
    # 尝试解析为字面量
    try:
        return int(param_expr)
    except (ValueError, TypeError):
        pass
    try:
        return float(param_expr)
    except (ValueError, TypeError):
        pass
    return param_expr


def _format_message(template: str, context: Dict, params: Dict, extra: Dict) -> str:
    """格式化消息模板，支持 {key} 变量替换"""
    fmt_dict = {}
    fmt_dict.update(params)
    fmt_dict.update(extra)
    fmt_dict['context'] = context

    # 先解析所有值为实际值
    resolved = {}
    for k, v in fmt_dict.items():
        resolved[k] = _resolve_param(v, context) if isinstance(v, str) else v

    try:
        return template.format(**resolved)
    except Exception:
        return template


class RuleEngine:
    """
    插件规则引擎。
    加载 YAML 规则文件，对巡检 context 执行规则检查。
    """

    def __init__(self, rules_dir: str = None):
        if rules_dir is None:
            # 默认相对于本文件
            from modules.core.paths import PROJECT_ROOT
            self.rules_dir = str(PROJECT_ROOT / 'modules/pro/rules')
        else:
            self.rules_dir = rules_dir

        self.builtin_rules: List[Dict] = []
        self.custom_rules: List[Dict] = []
        self.overrides: Dict = {}

        self._load_all()

    def _load_all(self):
        """加载所有规则"""
        builtin_dir = os.path.join(self.rules_dir, 'builtin')
        custom_dir = os.path.join(self.rules_dir, 'custom')
        override_file = os.path.join(self.rules_dir, 'overrides.yaml')

        self.builtin_rules = self._load_dir(builtin_dir)
        self.custom_rules = self._load_dir(custom_dir)
        self.overrides = self._load_overrides(override_file)

    @staticmethod
    def _load_dir(directory: str) -> List[Dict]:
        """加载目录下所有 YAML 规则文件"""
        rules = []
        if not os.path.isdir(directory):
            return rules
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(('.yaml', '.yml')):
                continue
            fpath = os.path.join(directory, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                if data and 'rules' in data:
                    for rule in data['rules']:
                        rule['_source'] = fpath
                        rules.append(rule)
            except Exception as e:
                print(f"[RuleEngine] 加载规则文件失败 {fname}: {e}")
        return rules

    @staticmethod
    def _load_overrides(filepath: str) -> Dict:
        """加载规则覆盖配置（禁用哪些规则）"""
        if not os.path.isfile(filepath):
            return {}
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def get_enabled_rules(self, db_type: str = None) -> List[Dict]:
        """
        获取启用的规则列表。
        合并内置规则和自定义规则，应用 overrides 配置。
        """
        all_rules = self.builtin_rules + self.custom_rules
        result = []

        disabled_ids = set(self.overrides.get('disabled_ids', []))

        canonical = normalize_db_type(db_type) if db_type else None
        for rule in all_rules:
            # 检查 db_type 过滤（两侧都归一化：pg/postgresql、dm/dm8、
            # oracle/oracle_jdbc、sqlserver/sqlserver_jdbc 视为同一库型）
            if canonical and rule.get('db_types'):
                rule_types = {normalize_db_type(t) for t in rule['db_types']}
                if canonical not in rule_types:
                    continue

            # 检查是否禁用
            rule_id = rule.get('id', '')
            if rule_id in disabled_ids:
                continue

            # 检查 enabled 字段
            if not rule.get('enabled', True):
                continue

            result.append(rule)

        # 按 priority 排序（high > medium > low）
        priority_order = {'high': 0, 'medium': 1, 'low': 2, 'info': 3}
        result.sort(key=lambda r: priority_order.get(r.get('priority', 'medium'), 1))

        # 同 id 去重：oracle/oracle_jdbc、mysql/mariadb 等文件间存在按库型复制的
        # 同名规则，归一化后会同时命中，只保留先出现的一条（避免重复告警）
        seen_ids = set()
        deduped = []
        for rule in result:
            rid = rule.get('id') or ''
            if rid and rid in seen_ids:
                continue
            if rid:
                seen_ids.add(rid)
            deduped.append(rule)
        return deduped

    def analyze(self, db_type: str, context: Dict) -> List[Dict]:
        """
        对 context 执行所有适用规则，返回 issues 列表。
        issues 格式兼容 analyzer.py 输出。
        """
        rules = self.get_enabled_rules(db_type)
        issues = []

        for rule in rules:
            issue = self._run_rule(rule, db_type, context)
            if issue:
                issues.append(issue)

        return issues

    def _run_rule(self, rule: Dict, db_type: str, context: Dict) -> Optional[Dict]:
        """
        执行单条规则。
        返回 issue dict 或 None（未触发）。
        """
        rule_id = rule.get('id', '')
        condition = rule.get('condition', '')
        params_raw = rule.get('params', {})

        if not condition:
            return None

        # 解析 params（支持 ${context.xxx} 引用）
        resolved_params = {}
        for k, v in params_raw.items():
            resolved_params[k] = _resolve_param(v, context)

        # 计算额外变量（如连接数使用率 conn_pct 等）
        extra = {}
        try:
            # 自动计算连接数使用率 conn_pct：
            # 当有 max_conn 且存在表示"已用连接"的参数（max_used 或 cur_conn）时计算
            if 'max_conn' in resolved_params:
                mc = resolved_params['max_conn']
                mu = resolved_params.get('max_used', resolved_params.get('cur_conn', None))
                if mu is not None and mc and mc > 0:
                    extra['conn_pct'] = mu / mc * 100
        except Exception:
            pass

        # 执行条件表达式。
        # 命名空间优先级：阈值基线 < 规则 params < 自动派生量（extra）。
        # 这样规则可以写 `tbs_usage_pct > db_tbs_usage_alert_pct`，
        # 而规则自身的 params 若显式给了同名变量，仍以规则为准。
        #
        # 实例级基线的入口：调用方把实例标识放进 context['_instance_id']，
        # 这里透传给 load_thresholds，让「自己跟自己比」的阈值覆盖全局值。
        # 取不到（CLI 裸跑、未注册端点）就退回全局阈值，行为与改前一致。
        _iid = context.get('_instance_id')
        _thresholds = load_thresholds(instance_id=_iid)
        ok, err = _safe_eval(condition, context,
                             {**_thresholds, **resolved_params, **extra})

        if not ok:
            return None

        # 条件触发，生成 issue
        severity = rule.get('severity', 'medium')

        # 中英文消息
        lang = context.get('_lang', 'zh')
        if lang == 'en' and 'message_en' in rule:
            message = rule['message_en']
        else:
            message = rule.get('message_zh', rule.get('message', ''))

        fix_sql = rule.get('fix_sql', '')

        # 格式化消息和 fix_sql
        fmt_vars = {**resolved_params, **extra}
        try:
            message = message.format(**fmt_vars)
        except Exception:
            pass
        try:
            fix_sql = fix_sql.format(**fmt_vars)
        except Exception:
            pass

        return {
            'col1': rule.get('name_zh', rule.get('name', rule_id)),
            'col2': self._severity_label(severity, lang),
            'col3': message,
            'col4': self._priority_label(rule.get('priority', 'medium'), lang),
            'col5': rule.get('owner', 'DBA'),
            'fix_sql': fix_sql,
            '_rule_id': rule_id,   # 内部标记，报告生成时去除
        }

    @staticmethod
    def _severity_label(severity: str, lang: str) -> str:
        labels = {
            'high':   {'zh': '高风险', 'en': 'High Risk'},
            'medium': {'zh': '中风险', 'en': 'Medium Risk'},
            'low':    {'zh': '低风险', 'en': 'Low Risk'},
            'info':   {'zh': '建议',   'en': 'Suggestion'},
        }
        return labels.get(severity, {}).get(lang, severity)

    @staticmethod
    def _priority_label(priority: str, lang: str) -> str:
        labels = {
            'high':   {'zh': '高', 'en': 'High'},
            'medium': {'zh': '中', 'en': 'Medium'},
            'low':    {'zh': '低', 'en': 'Low'},
        }
        return labels.get(priority, {}).get(lang, priority)

    # ── 规则管理 API（供 web_ui.py 调用）────────────────

    def list_rules(self, db_type: str = None) -> List[Dict]:
        """列出所有规则（含启用状态）"""
        all_rules = self.builtin_rules + self.custom_rules
        disabled_ids = set(self.overrides.get('disabled_ids', []))

        result = []
        for rule in all_rules:
            if db_type and rule.get('db_types'):
                if db_type not in rule['db_types']:
                    continue
            rule_copy = rule.copy()
            is_disabled = rule.get('id', '') in disabled_ids
            rule_copy['_disabled'] = is_disabled
            rule_copy['enabled'] = not is_disabled  # 添加 enabled 字段
            result.append(rule_copy)
        return result

    def get_rule(self, rule_id: str) -> Dict | None:
        """按 ID 获取单条规则（含合并后的 enabled 状态）"""
        for rule in self.list_rules():
            if rule.get('id') == rule_id:
                return rule
        return None

    def toggle_rule(self, rule_id: str, enabled: bool) -> bool:
        """启用/禁用规则（写入 overrides.yaml）"""
        overrides = dict(self.overrides)
        disabled = set(overrides.get('disabled_ids', []))

        if enabled:
            disabled.discard(rule_id)
        else:
            disabled.add(rule_id)

        overrides['disabled_ids'] = list(disabled)
        overrides['updated_at'] = __import__('datetime').datetime.now().isoformat()

        override_file = os.path.join(self.rules_dir, 'overrides.yaml')
        os.makedirs(os.path.dirname(override_file), exist_ok=True)
        with open(override_file, 'w', encoding='utf-8') as f:
            yaml.dump(overrides, f, allow_unicode=True, sort_keys=False)

        self.overrides = overrides
        return True

    def save_custom_rule(self, rule: Dict) -> bool:
        """保存自定义规则到 custom/ 目录"""
        rule_id = rule.get('id', '')
        if not rule_id:
            return False

        custom_dir = os.path.join(self.rules_dir, 'custom')
        os.makedirs(custom_dir, exist_ok=True)

        # 读取现有 custom 规则
        all_custom = []
        for fname in sorted(os.listdir(custom_dir)):
            if not fname.endswith(('.yaml', '.yml')):
                continue
            fpath = os.path.join(custom_dir, fname)
            with open(fpath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if data and 'rules' in data:
                all_custom.extend(data['rules'])

        # 去重（按 id）
        all_custom = [r for r in all_custom if r.get('id') != rule_id]
        all_custom.append(rule)

        # 写入文件（一个规则一个文件，或合并到一个文件）
        out_file = os.path.join(custom_dir, f"{rule_id}.yaml")
        with open(out_file, 'w', encoding='utf-8') as f:
            yaml.dump({'rules': [rule]}, f, allow_unicode=True, sort_keys=False)

        self._load_all()
        return True

    def delete_custom_rule(self, rule_id: str) -> bool:
        """删除自定义规则"""
        custom_dir = os.path.join(self.rules_dir, 'custom')
        target = os.path.join(custom_dir, f"{rule_id}.yaml")
        if os.path.isfile(target):
            os.remove(target)
            self._load_all()
            return True
        return False


# ── 便捷函数（供 analyzer.py 调用）─────────────────────────

_cache: Optional[RuleEngine] = None

# ── 规则取数层（metrics）──────────────────────────────────────────────────
# 规则 params 引用的 context 键与巡检模板 query_key 是两套命名，历史上大量规则
# 因取不到数而静默跳过（详见 scripts/gen_rule_metrics.py 头注）。metrics 层在
# 巡检采集后补齐缺失的键：模板已提供（非空）的键不重复采集，因此与任何模板兼容。

_METRICS_CACHE = {}


def load_metrics(db_type):
    """加载某产品的取数清单（modules/pro/rules/metrics/<规范库名>.yaml）。"""
    canonical = normalize_db_type(db_type)
    if canonical in _METRICS_CACHE:
        return _METRICS_CACHE[canonical]
    from modules.core.paths import PROJECT_ROOT
    mdir = str(PROJECT_ROOT / 'modules/pro/rules/metrics')
    path = os.path.join(mdir, canonical + '.yaml')
    # 兜底：metrics 文件名可能按「规则文件名」而非「规范库名」命名
    # （例：规则文件 yasdb.yaml 声明 db_types ['yashandb']，metrics 却叫 yasdb.yaml）。
    # 此时按文件内 db_type 字段反查，避免取数层静默取不到任何指标。
    if not os.path.isfile(path) and os.path.isdir(mdir):
        for fn in os.listdir(mdir):
            if not fn.endswith('.yaml'):
                continue
            try:
                with open(os.path.join(mdir, fn), 'r', encoding='utf-8') as fh:
                    head = yaml.safe_load(fh) or {}
                # 两侧都归一化：文件可能写 yasdb，而 canonical 是 yashandb
                if normalize_db_type(head.get('db_type')) == canonical:
                    path = os.path.join(mdir, fn)
                    break
            except Exception:
                continue
    data = []
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                loaded = yaml.safe_load(fh) or {}
            data = loaded.get('metrics') or []
        except Exception as e:
            print('[RuleEngine] 加载 metrics 失败 %s: %s' % (path, e))
    _METRICS_CACHE[canonical] = data
    return data


# ── 指标阈值（§4.6 基线）注入条件表达式 ────────────────────────────────
# 规则 condition 里不再硬编码 80 / 90 / 95 这类魔法数字，而是直接引用
# server_thresholds 的键名，例如：
#     condition: "tbs_usage_pct > db_tbs_usage_alert_pct"
# 用户在 Web UI 改阈值后，规则判定立即跟着变，无需改 YAML。
#
# 两级查找：先取全局 server_thresholds，再用 instance_thresholds 覆盖。
# 实例级阈值由 scripts/build_self_baselines.py 按月按实例算出
# （实例阈值 = max(全局值, 自身 P95 × 倍数)），只覆盖它算得出的那几个键，
# 其余键仍走全局值——所以必须「叠加」而不是「替换」。
_THRESHOLD_CACHE = {'mtime': None, 'global': None, 'instances': {}}


def _safe_float(value):
    """阈值只接受可转 float 的值，其余（None / 空串 / 中文）一律丢弃。

    丢弃而非置 0：0 会参与比较并制造误报（`x > 0` 恒真）。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_global_thresholds(conn):
    """读全局阈值（server_thresholds）。"""
    data = {}
    try:
        cur = conn.cursor()
        cur.execute('SELECT key, value FROM server_thresholds')
        for key, value in cur.fetchall():
            if not isinstance(key, str) or not key.isidentifier():
                continue
            fv = _safe_float(value)
            if fv is not None:
                data[key] = fv
    except Exception:
        # 表不存在 / 被锁：退回空字典，规则因变量未定义而跳过，不误报
        return {}
    return data


def _load_instance_thresholds(conn, instance_id):
    """读某实例的自身基线覆盖值（instance_thresholds）。

    表缺失（老库、还没跑过 build_self_baselines.py）按「无覆盖」处理，
    返回空字典而不是抛错——否则升级瞬间全库规则都会被跳过。
    """
    data = {}
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT key, value FROM instance_thresholds WHERE instance_id = ?',
            (str(instance_id),))
        for key, value in cur.fetchall():
            if not isinstance(key, str) or not key.isidentifier():
                continue
            fv = _safe_float(value)
            if fv is not None:
                data[key] = fv
    except Exception:
        return {}
    return data


def load_thresholds(db_path=None, instance_id=None):
    """加载业务指标阈值，带 mtime 缓存。

    优先级：``instance_thresholds``（实例自身基线）> ``server_thresholds``
    （全局基线）。全局键永远保留，实例键只做定向覆盖。

    只暴露「合法 Python 标识符 + 数值」的键，避免把含空格/百分号的键
    注入 eval 命名空间。加载失败返回空字典——规则会因变量未定义而跳过，
    不会误报。
    """
    from modules.core.paths import INSPECTION_DB
    default_path = str(INSPECTION_DB)
    path = db_path or default_path
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}

    # 缓存按库文件 mtime 失效；实例覆盖值存在 instances 子字典里，
    # 同一份库文件可服务多个实例，不必每次重查。
    if _THRESHOLD_CACHE['mtime'] != mtime:
        _THRESHOLD_CACHE['mtime'] = mtime
        _THRESHOLD_CACHE['global'] = None
        _THRESHOLD_CACHE['instances'] = {}

    iid = str(instance_id).strip() if instance_id else ''
    if not iid:
        if _THRESHOLD_CACHE['global'] is None:
            try:
                import sqlite3
                conn = sqlite3.connect(path)
                try:
                    _THRESHOLD_CACHE['global'] = _load_global_thresholds(conn)
                finally:
                    conn.close()
            except Exception as e:
                print('[RuleEngine] 加载阈值失败 %s: %s' % (path, e))
                _THRESHOLD_CACHE['global'] = {}
        return _THRESHOLD_CACHE['global']

    if iid in _THRESHOLD_CACHE['instances']:
        return _THRESHOLD_CACHE['instances'][iid]

    # 复用上面的全局加载与缓存，再叠加实例覆盖值。
    # dict(...) 拷贝一份再 update，避免把实例值写进被共享的全局缓存。
    merged = dict(load_thresholds(path))
    try:
        import sqlite3
        conn = sqlite3.connect(path)
        try:
            overrides = _load_instance_thresholds(conn, iid)
        finally:
            conn.close()
    except Exception as e:
        print('[RuleEngine] 加载实例阈值失败 %s(%s): %s' % (path, iid, e))
        overrides = {}
    merged.update(overrides)
    _THRESHOLD_CACHE['instances'][iid] = merged
    return merged


def _rows_to_dicts(cursor, limit=200):
    """把游标结果统一为 list[dict]（兼容 tuple 行与具名行）。"""
    cols = None
    if cursor.description:
        cols = [c[0] if isinstance(c, (tuple, list)) else c.name
                for c in cursor.description]
    data = []
    for row in cursor.fetchall()[:limit]:
        if hasattr(row, 'keys'):
            data.append({k: row[k] for k in row.keys()})
        else:
            data.append(dict(zip(cols or [], row)))
    return data, cols


def _derive_system_metrics(context, derived):
    """从 system_info 派生「与库型无关」的系统资源指标（零 SQL）。

    §4.6 的 CPU 平均利用率对任何产品都是同一个物理量，没必要为 22 个产品
    各写一条 SQL——它本来就在 system_info 里（见 modules/server/inspect.py）：

        cpu.usage_percent / memory.usage_percent / memory.swap_usage_percent

    采集不到（未启用 SSH 查询、连不上目标主机）就**不生成**对应键，
    规则会因变量未定义而跳过——不写 0，避免把「没采到」判成「零负载」。
    """
    si = context.get('system_info')
    if not isinstance(si, dict) or not si:
        return

    def _num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f else f  # 挡掉 NaN

    cpu = si.get('cpu') or {}
    mem = si.get('memory') or {}

    pairs = (
        ('cpu_avg_pct', cpu.get('usage_percent')),
        ('mem_usage_pct', mem.get('usage_percent')),
        ('swap_usage_pct', mem.get('swap_usage_percent')),
    )
    for key, raw in pairs:
        if key in context or key in derived:
            continue
        val = _num(raw)
        if val is not None:
            derived[key] = round(val, 2)


def _derive_metrics(canonical, context):
    """少量无法用单条 SQL 表达的派生键（形状由多个采集项计算得出）。"""
    derived = {}
    # 系统资源（CPU / 内存 / swap）：所有库型通用，来源 system_info
    _derive_system_metrics(context, derived)
    # Redis：连接使用率 = connected_clients / maxclients * 100
    if canonical in ('redis', 'redis-cluster') and 'redis_conn_pct' not in context:
        try:
            stats = context.get('redis_stats') or []
            cfg = context.get('redis_config') or []
            cur = int(stats[0].get('CONNECTED_CLIENTS') or 0) if stats else 0
            mc = 0
            for c in cfg:
                if str(c.get('PARAM_NAME', '')).lower() == 'maxclients':
                    mc = int(float(c.get('PARA_VALUE') or c.get('VALUE') or 0))
                    break
            if mc > 0:
                derived['redis_conn_pct'] = round(cur / mc * 100, 2)
        except Exception:
            pass
    # GBase 8a：连接使用率 = 会话总数 / max_connections * 100
    if canonical == 'gbase8a' and 'gbase8a_conn_pct' not in context:
        try:
            used = int(float(context.get('gbase8a_conn_used') or 0))
            limit = int(float(context.get('gbase8a_max_conn') or 0))
            if limit > 0:
                derived['gbase8a_conn_pct'] = round(used / limit * 100, 2)
        except Exception:
            pass
    return derived


def fill_missing_metrics(db_type, context, conn):
    """在巡检采集后补齐规则所需的 context 键。

    语义：
    - 只补缺失/为空的键（模板已采到的不动）；
    - 每条 SQL 过 ``data_security.assert_readonly_sql`` 只读校验；
    - 单条失败静默跳过（对应规则不触发），绝不影响巡检主流程。
    """
    # 注意：必须用 is None 判断——空 dict 是合法的 context（首次采集时可能确实为空），
    # 写成 `if not context` 会让取数层对空 context 直接短路，规则永远取不到数。
    if context is None or conn is None:
        return 0
    canonical = normalize_db_type(db_type)
    metrics = load_metrics(canonical)
    filled = 0
    try:
        from modules.core.data_security import assert_readonly_sql
    except Exception:
        assert_readonly_sql = None

    cur = None
    try:
        cur = conn.cursor()
    except Exception:
        return 0

    try:
        for m in metrics:
            key = m.get('key')
            if not key:
                continue
            existing = context.get(key)
            if existing not in (None, [], {}, ''):
                continue  # 模板已提供
            sql = (m.get('sql') or '').strip()
            if not sql:
                continue
            if assert_readonly_sql is not None:
                try:
                    assert_readonly_sql(sql)
                except Exception:
                    continue
            try:
                cur.execute(sql.strip().rstrip(';').strip())
                data, cols = _rows_to_dicts(cur)
                shape = m.get('shape', 'rows')
                if shape == 'scalar':
                    context[key] = (data[0][cols[0]]
                                    if data and cols else None)
                elif shape == 'map':
                    kc = m.get('map_key', 'name')
                    vc = m.get('map_value', 'value')
                    mapping = {}
                    for row in data:
                        mk = str(row.get(kc, '')).strip()
                        if not mk:
                            continue
                        if m.get('map_case', 'asis') == 'upper':
                            mk = mk.upper()
                        elif m.get('map_case', 'asis') == 'lower':
                            mk = mk.lower()
                        mapping[mk] = row.get(vc)
                    context[key] = mapping
                else:
                    context[key] = data
                context.setdefault('_metric_status', {})[key] = {'status': 'collected' if context.get(key) is not None else 'unavailable', 'rows': len(data)}
                filled += 1
            except Exception as error:
                context.setdefault('_metric_status', {})[key] = {'status': 'error', 'reason': str(error)[:300]}
                continue
    finally:
        try:
            cur.close()
        except Exception:
            pass

    # 派生键（依赖上面刚补齐/模板已有的采集项）
    for k, v in _derive_metrics(canonical, context).items():
        context[k] = v
        filled += 1
    return filled


def get_rule_engine() -> RuleEngine:
    """获取全局 RuleEngine 实例（单例）"""
    global _cache
    if _cache is None:
        _cache = RuleEngine()
    return _cache


def analyze_with_plugins(db_type: str, context: Dict) -> List[Dict]:
    """
    供 analyzer.py 调用的便捷函数。
    执行所有插件规则，返回 issues 列表。
    """
    try:
        engine = get_rule_engine()
        return engine.analyze(db_type, context)
    except Exception as e:
        print(f"[RuleEngine] 插件规则执行失败: {e}")
        return []


if __name__ == '__main__':
    # 测试
    engine = RuleEngine()
    print(f"已加载 {len(engine.builtin_rules)} 条内置规则，{len(engine.custom_rules)} 条自定义规则")

    # 测试规则执行
    test_context = {
        'max_connections': [{'Value': '200'}],
        'max_used_connections': [{'Value': '180'}],
    }
    issues = engine.analyze('mysql', test_context)
    print(f"测试触发 {len(issues)} 条规则")
    for iss in issues:
        print(f"  - {iss['col1']}: {iss['col3'][:60]}")
