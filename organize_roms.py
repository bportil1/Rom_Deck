#!/usr/bin/env python3
"""Portable ROM/Emulator organizer, copy-only and dry-run-first.

Usage:
  python organize_roms.py plan --source PATH --destination PATH --output plan.json
  python organize_roms.py apply --plan plan.json
  python organize_roms.py apply --plan plan.json --verify
  python organize_roms.py plan --inventory tree.json --source PATH --destination PATH --output preview.json
"""
import argparse
import json
from pathlib import Path
from romdeck_core import from_inventory, from_filesystem, build_plan, save_plan, apply_plan


def main():
    parser = argparse.ArgumentParser(description='Local ROM / emulator organizer (non-destructive).')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan', help='Scan and generate a proposed organization plan; copies nothing.')
    p.add_argument('--source', required=True, help='Original collection root')
    p.add_argument('--destination', required=True, help='Separate directory for organized copy')
    p.add_argument('--output', default='romdeck_plan.json')
    p.add_argument('--inventory', help='Optional tree -J JSON input (preview only; cannot apply)')
    p.add_argument('--overrides', help='JSON object mapping source-relative paths to console names')
    a = sub.add_parser('apply', help='Copy approved items from a live plan; no deletion or overwrite.')
    a.add_argument('--plan', required=True)
    a.add_argument('--verify', action='store_true', help='SHA-256 verify every copied ROM (slower).')
    a.add_argument('--yes', action='store_true', help='Confirm copying without interactive prompt.')
    args = parser.parse_args()
    if args.command == 'plan':
        if args.inventory:
            _root, files = from_inventory(Path(args.inventory))
        else:
            _root, files = from_filesystem(Path(args.source))
        overrides = json.loads(Path(args.overrides).read_text(encoding='utf-8')) if args.overrides else None
        plan = build_plan(args.source, args.destination, files, inventory_only=bool(args.inventory), overrides=overrides)
        save_plan(plan, Path(args.output))
        print(json.dumps(plan['summary'], indent=2))
        print(f"Plan written: {args.output}")
        if args.inventory:
            print('Inventory preview only. It cannot be applied until the real files are scanned.')
        print('No source files were modified.')
    else:
        plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
        if plan.get('inventory_only'):
            parser.error('Inventory preview cannot be applied. Generate a live plan on the machine with the files.')
        print(f"Source:      {plan['source']}")
        print(f"Destination: {plan['destination']}")
        print(f"Planned items: {len(plan['items'])}; no source deletions, no overwrites")
        if not args.yes:
            if input('Type COPY to continue: ').strip() != 'COPY':
                print('Cancelled, nothing copied.')
                return
        report = apply_plan(plan, verify=args.verify,
            progress=lambda i,n,name: print(f'[{i}/{n}] {name}', flush=True) if i % 100 == 0 or i == n else None)
        report_path = Path(args.plan).with_name(Path(args.plan).stem + '_results.json')
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report['counts'], indent=2))
        print(f'Copy report: {report_path}')
        print('Original source untouched.')
        if report['errors']:
            raise SystemExit(1)

if __name__ == '__main__':
    main()
