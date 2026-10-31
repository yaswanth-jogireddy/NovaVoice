"""One trusted catalog for local and hosted browser demos."""
import json
from pathlib import Path
import re

CATALOG_VERSION = '2026-10-19-us-saudi-property'
# The browser shows call costs in each demo's own currency. The market comes from the
# demo's knowledge file (knowledge/markets.py); India is the default.
# Choosing a demo also picks its callers' language ('en_us' = English with the US voice);
# the browser falls back to English when that language cannot be spoken yet.
MARKET_CURRENCY = {'US': 'USD', 'SA': 'SAR'}
MARKET_LANGUAGE = {'US': 'en_us', 'SA': 'ar'}
KNOWLEDGE_DIR = Path(__file__).with_name('knowledge') / 'data'


def preset_market(preset_id):
    source = KNOWLEDGE_DIR / f'{preset_id}.json'
    return json.loads(source.read_text(encoding='utf-8')).get('market') if source.exists() else None


def load_presets(path=None):
    source = Path(path) if path else Path(__file__).with_name('demo_presets.json')
    rows = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(rows, list) or not rows:
        raise ValueError('Demo catalog must be a nonempty list')
    seen = set()
    required = ('id', 'name', 'title', 'business_name', 'tag', 'description',
                'outcome', 'opener', 'challenge', 'opening_question', 'scenario', 'context')
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in required):
            raise ValueError('Incomplete demo preset')
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', row['id']) or row['id'] in seen:
            raise ValueError('Invalid or duplicate demo preset ID')
        seen.add(row['id'])
        # These are business-specific profiles; generic workflows must not add
        # unrelated requirements such as qualifying voice-agent software sales.
        if row['scenario'] != 'custom':
            raise ValueError('Business demo presets must use the custom workflow')
        row['catalog_version'] = CATALOG_VERSION
        market = preset_market(row['id'])
        row['currency'] = MARKET_CURRENCY.get(market, 'INR')
        row['language'] = MARKET_LANGUAGE.get(market, '')
        row['context'] += (
            '\nOpening: introduce Maya as an AI assistant and this business as a fictional demo, '
            'then adapt this opening question to the selected language: ' + row['opening_question'] +
            '\nCompletion: only the essentials above are required. Treat explicitly unknown or declined '
            'details as resolved; do not invent more mandatory fields. When these details are collected, '
            'answer any pending question and follow CALL COMPLETION for the one final specific-request '
            'question. A broad requested time period is enough; do not require an exact hour. '
            'Preserve extra requests without promising external action. If the caller changes topic, '
            'pause this intake and address their actual purpose within honest limits.'
        )
        if len(row['context']) > 4000:
            raise ValueError('Demo context exceeds the session limit')
    return rows


DEMO_PRESETS = load_presets()


def get_preset(preset_id):
    if not preset_id:
        return None
    preset = next((row for row in DEMO_PRESETS if row['id'] == preset_id), None)
    if preset is None:
        raise ValueError('Choose an available demo use case')
    return preset
