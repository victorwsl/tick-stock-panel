"""个人知行研究策略的显式安装；重复运行不覆盖手工修改。"""
from pathlib import Path


def install_strategy(data_dir: Path) -> dict:
    source = Path(__file__).resolve().parents[1] / 'strategy' / 'personal' / 'zhixing_pullback.py'
    content = source.read_text(encoding='utf-8')
    target = data_dir / 'strategies' / 'custom' / 'personal_zhixing_pullback_v0.py'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_text(encoding='utf-8') != content:
            raise ValueError('策略安装文件已有修改，未覆盖：' + str(target))
    else:
        with target.open('x', encoding='utf-8') as stream:
            stream.write(content)
    return {'strategy_id': 'personal_zhixing_pullback_v0', 'path': str(target),
            'status': 'signal_experiment_only', 'partial_exit_implemented': False,
            'open_rr_recheck_implemented': False, 'production_ready': False}


def install_early_strategy(data_dir: Path) -> dict:
    source = Path(__file__).resolve().parents[1] / 'strategy' / 'personal' / 'zhixing_early.py'
    content = source.read_text(encoding='utf-8')
    target = data_dir / 'strategies' / 'custom' / 'personal_zhixing_early_v0.py'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_text(encoding='utf-8') != content:
            raise ValueError('策略安装文件已有修改，未覆盖：' + str(target))
    else:
        with target.open('x', encoding='utf-8') as stream:
            stream.write(content)
    return {'strategy_id': 'personal_zhixing_early_v0', 'path': str(target),
            'status': 'research_only', 'production_ready': False}
