"""Normalize one JSON transport fence, without repairing or relaxing schemas."""
import json
import re


def normalize(text):
    if not isinstance(text,str):raise ValueError('json_output_not_text')
    stripped=text.strip()
    fenced=re.fullmatch(r'```(?:json)?\s*\n([\s\S]*?)\n```',stripped,flags=re.I)
    if fenced and (stripped.lower().startswith('```json') or fenced[1].lstrip().startswith('{')):
        text=fenced[1].strip();stripped=text
        if not stripped.startswith('{'):raise ValueError('json_output_not_object')
    elif stripped.lower().startswith('```json'):raise ValueError('json_output_envelope_invalid')
    if stripped.startswith('{'):
        def distinct(pairs):
            value={}
            for key,item in pairs:
                if key in value:raise ValueError('duplicate_json_key')
                value[key]=item
            return value
        json.loads(stripped,object_pairs_hook=distinct)
    return text
