"""Local-only inventory, classification, and safe copying for RomDeck.

Python 3.10+. No network calls. Safe ZIP inspection and optional extraction. No deletion of originals.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
import uuid
from typing import Callable

PLATFORMS = (
    "NES", "Famicom Disk System", "SNES", "Nintendo 64", "Game Boy",
    "Game Boy Color", "Game Boy Advance", "Nintendo DS", "Nintendo 3DS",
    "GameCube", "Wii", "Wii U", "Nintendo Switch", "Sega Master System",
    "Game Gear", "Sega Genesis", "Sega 32X", "Sega CD", "Sega Saturn", "TurboGrafx-16", "Atari 2600",
    "PlayStation", "PlayStation 2", "PlayStation 3", "PSP",
    "Pokémon Mini", "Satellaview", "Sufami Turbo", "Virtual Boy", "Other",
)

# Only unambiguous, directly playable file extensions are auto-classified.
EXTENSIONS = {
    ".nes": "NES", ".fds": "Famicom Disk System", ".sfc": "SNES", ".smc": "SNES",
    ".n64": "Nintendo 64", ".z64": "Nintendo 64", ".v64": "Nintendo 64",
    ".gb": "Game Boy", ".gbc": "Game Boy Color", ".gba": "Game Boy Advance",
    ".nds": "Nintendo DS", ".3ds": "Nintendo 3DS", ".cci": "Nintendo 3DS",
    ".sms": "Sega Master System", ".gg": "Game Gear", ".pce": "TurboGrafx-16",
    ".gen": "Sega Genesis", ".smd": "Sega Genesis",
    ".32x": "Sega 32X", ".wbfs": "Wii", ".wud": "Wii U", ".wux": "Wii U",
    ".nsp": "Nintendo Switch", ".xci": "Nintendo Switch",
    ".cso": "PSP", ".min": "Pokémon Mini", ".vb": "Virtual Boy",
    ".st": "Sufami Turbo", ".bs": "Satellaview", ".gcm": "GameCube",
}
ARCHIVES = {".zip", ".7z", ".rar"}
AMBIGUOUS_IMAGES = {".iso", ".gcz", ".rvz", ".chd", ".cue", ".bin", ".img", ".rom"}
KNOWN_EMULATORS = {
    "dolphin.exe": ("Dolphin", ("GameCube", "Wii"), "-b -e {rom}"),
    "bsnes.exe": ("bsnes", ("SNES",), "{rom}"),
    "no$gba.exe": ("NO$GBA", ("Nintendo DS", "Game Boy Advance"), "{rom}"),
    "gbe_plus_qt.exe": ("GBE+", ("Game Boy", "Game Boy Color", "Game Boy Advance", "Game Gear", "Sega Master System"), ""),
    "gbe_plus.exe": ("GBE+", ("Game Boy", "Game Boy Color", "Game Boy Advance", "Game Gear", "Sega Master System"), ""),
    "rpcs3.exe": ("RPCS3", ("PlayStation 3",), ""),
    "dolphin-emu": ("Dolphin", ("GameCube", "Wii"), "-b -e {rom}"),
    "bsnes": ("bsnes", ("SNES",), "{rom}"),
    "rpcs3": ("RPCS3", ("PlayStation 3",), ""),
}
# Prefer normal GUI programs to auxiliary/CLI executables.
EMULATOR_PRIORITY = {"gbe_plus_qt.exe": 10, "gbe_plus.exe": 2}

# Path segments are compared case-insensitively. Preserve them in place.
PROTECTED_SEGMENTS = {
    "cache", "statesaves", "state saves", "backup", "saves", "battery", "slot",
    "config", "configs", "screenshots", "screenshots", "shaders", "filters",
    "textures", "resourcepacks", "resources", "logs", "docs", "documents",
    "sys", "dev_hdd0", "gamelist", "bios files (if needed)", "bios", "firmware",
    "flashcard users", "flashcart loader", "flashcart users", "pspsnesemu", "twilightshit", "twilightmenu",
    "_nds", "__rpg", "_wfwd", "_afwd", "_dstwo", "sdnand root", "title",
    "3ds - cfw users", "dsi - cfw users", "dsi&3ds - sd card users",
    "dolphin emulator",  # This is Dolphin user data, not its executable installation.
}

# System names appearing in directory segments, ordered most specific first.
CONTEXT_HINTS = (
    ("nintendo - game boy advance", "Game Boy Advance"),
    ("game boy advance", "Game Boy Advance"), ("gameboy advance", "Game Boy Advance"),
    ("nintendo - game boy color", "Game Boy Color"),
    ("game boy color", "Game Boy Color"), ("gameboy color", "Game Boy Color"),
    ("nintendo - game boy", "Game Boy"), ("gameboy", "Game Boy"), ("game boy", "Game Boy"),
    ("famicom disk system", "Famicom Disk System"), ("nintendo entertainment system", "NES"), ("nintendo 64", "Nintendo 64"),
    ("n64", "Nintendo 64"), ("sega master system", "Sega Master System"),
    ("master system", "Sega Master System"), ("game gear", "Game Gear"),
    ("sega genesis", "Sega Genesis"), ("sega 32x", "Sega 32X"),
    ("turbografx-16", "TurboGrafx-16"), ("turbografx", "TurboGrafx-16"),
    ("atari 2600", "Atari 2600"), ("snes", "SNES"),
    ("super nintendo", "SNES"), ("nes", "NES"),
    ("nintendo 3ds", "Nintendo 3DS"), ("nintendo ds", "Nintendo DS"),
    ("ds", "Nintendo DS"),
    ("psproms", "PSP"), ("psp roms", "PSP"),
    ("playstation 3", "PlayStation 3"), ("playstation 2", "PlayStation 2"),
    ("playstation", "PlayStation"),
    ("gamecubeemulator", "GameCube"), ("gamecube", "GameCube"),
    ("gba emu", "Game Boy Advance"), ("bsnes_v075-64bit", "SNES"),
)


def norm_rel(value: str) -> str:
    """Normalize paths for storage, rejecting absolute and traversing components."""
    value = value.replace('\\', '/')
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or not value or any(s in ('.', '..', '') for s in value.split('/')):
        raise ValueError(f"Unsafe relative path: {value!r}")
    return parsed.as_posix()


def from_inventory(path: Path):
    """Read a tree -J JSON report. The report is not a live filesystem."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data or data[0].get('type') != 'directory':
        raise ValueError('Expected tree -J output (list with one directory root).')
    root = data[0]
    entries = []
    def walk(n, prefix):
        for child in n.get('contents', []):
            rel = norm_rel('/'.join(prefix + (child['name'],)))
            if child.get('type') == 'file':
                entries.append(rel)
            elif child.get('type') == 'directory':
                walk(child, prefix + (child['name'],))
    walk(root, ())
    return root['name'], sorted(entries, key=str.casefold)


def from_filesystem(root: Path):
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Source root must be a directory.')
    files = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in names:
            full = Path(base) / name
            if full.is_symlink() or not full.is_file():
                continue
            files.append(norm_rel(full.relative_to(root).as_posix()))
    return str(root), sorted(files, key=str.casefold)


def discover_emulators(files: list[str]) -> list[dict]:
    groups = {}
    for rel in files:
        name = PurePosixPath(rel).name.lower()
        if name not in KNOWN_EMULATORS:
            continue
        label, platforms, template = KNOWN_EMULATORS[name]
        folder = str(PurePosixPath(rel).parent)
        key = (label, folder)
        score = EMULATOR_PRIORITY.get(name, 5)
        if key not in groups or score > groups[key]['priority']:
            groups[key] = {
                'name': label, 'relative_executable': rel,
                'directory': folder, 'platforms': list(platforms),
                'arguments': template, 'priority': score,
            }
    return sorted(groups.values(), key=lambda d: (d['name'].lower(), d['directory'].lower()))


# Archive safety limits are deliberately conservative. Increase only after inspection.
MAX_ZIP_ENTRIES = 30_000
MAX_ZIP_BYTES = 16 * 1024**3
MAX_MEMBER_BYTES = 8 * 1024**3
MAX_COMPRESSION_RATIO = 1000
BAD_ZIP_FILES = {".exe", ".dll", ".msi", ".bat", ".cmd", ".ps1", ".swf", ".jar", ".apk", ".cia", ".3dsx"}
ZIP_HELPERS = {".txt", ".nfo", ".md", ".jpg", ".jpeg", ".png", ".xml", ".html", ".sfv", ".diz", ".sav", ".srm"}
DISC_FORMATS = {".iso", ".gcz", ".rvz", ".ciso", ".chd", ".img", ".bin", ".cue"}
NON_GAME_PACKAGE_TOKENS = (
    "firmware", "bios", "boot9strap", "installer", "luma3ds", "infinity-2", "pro-c2", "lme-2",
    "flashcard", "flashcart", "twilightmenu", "temp-gba", "tempgba",
    "cfw", "gba_bios", "patcher", "r4i", "homebrew channel",
)
# Targeted title hints help assign shared disc formats; archive membership still checked.
TITLE_CONSOLE_HINTS = (
    ("castlevania - symphony of the night", "PlayStation"),
    ("fire emblem - radiant dawn", "Wii"),
    ("fire emblem - path of radiance", "GameCube"),
    ("fire emblem the binding blade", "Game Boy Advance"),
    ("fire emblem the sacred stones", "Game Boy Advance"),
    ("golden sun", "Game Boy Advance"),
    ("pokemon - soulsilver", "Nintendo DS"),
    ("pokemon - emerald", "Game Boy Advance"),
    ("pokemon - leaf green", "Game Boy Advance"),
)


def title_hint(name):
    low = name.casefold()
    for needle, console in TITLE_CONSOLE_HINTS:
        if needle in low:
            return console
    return None


def segment_hint(parts):
    # Search from deepest directory; explicit handling of nested Nintendo names
    # prevents "Super Nintendo Entertainment System" being misread as NES.
    for part in reversed(parts):
        raw = part.casefold().replace("_", " ").replace("-", " ")
        clean = " ".join(raw.split())
        if "super nintendo" in clean or "snes" in clean: return "SNES"
        if "pokemon mini" in clean: return "Pokémon Mini"
        if "satellaview" in clean: return "Satellaview"
        if "sufami turbo" in clean: return "Sufami Turbo"
        if "virtual boy" in clean: return "Virtual Boy"
        if "game boy advance" in clean or "gameboy advance" in clean: return "Game Boy Advance"
        if "game boy color" in clean or "gameboy color" in clean: return "Game Boy Color"
        if "game boy" in clean or "gameboy" in clean: return "Game Boy"
        if "nintendo entertainment system" in clean or clean == "nes": return "NES"
        if "nintendo 64" in clean or clean == "n64": return "Nintendo 64"
        if "nintendo 3ds" in clean: return "Nintendo 3DS"
        if "nintendo ds" in clean or clean == "ds": return "Nintendo DS"
        if "famicom disk system" in clean: return "Famicom Disk System"
        if "sega genesis" in clean: return "Sega Genesis"
        if "sega 32x" in clean: return "Sega 32X"
        if "sega cd" in clean: return "Sega CD"
        if "sega saturn" in clean or clean == "saturn": return "Sega Saturn"
        if "sega master system" in clean or "master system" in clean: return "Sega Master System"
        if "game gear" in clean: return "Game Gear"
        if "turbografx" in clean: return "TurboGrafx-16"
        if "atari 2600" in clean: return "Atari 2600"
        if clean == "psproms" or clean.startswith("psp roms"): return "PSP"
        if "playstation 3" in clean: return "PlayStation 3"
        if "playstation 2" in clean: return "PlayStation 2"
        if "playstation" in clean: return "PlayStation"
        if "gamecubeemulator" in clean or "gamecube" in clean: return "GameCube"
        if clean == "wii": return "Wii"
        if clean == "gba emu": return "Game Boy Advance"
        if clean.startswith("bsnes v"): return "SNES"
    return None


def protected_path(rel):
    parts = [x.casefold() for x in PurePosixPath(rel).parts]
    return ("ds3" in parts or "ps3emu" in parts or
            any(p in PROTECTED_SEGMENTS or p.strip("[]() ") in {"bios", "firmware"} for p in parts[:-1]))


def classify_file(rel: str, emulator_dirs: list[str], overrides=None):
    """Classify only loose files. A ZIP is never presumed to be a ROM."""
    overrides = overrides or {}
    p = PurePosixPath(rel)
    name = p.name.casefold()
    extension = p.suffix.casefold()
    if protected_path(rel):
        return 'support', None, 'Console firmware, flashcart utility, save, cache, or emulator support path'
    for emudir in emulator_dirs:
        if rel.casefold().startswith(emudir.rstrip('/').casefold() + '/'):
            remainder = rel[len(emudir.rstrip('/')) + 1:]
            if '/' in remainder:
                return 'support', None, 'Inside emulator installation (preserved with emulator)'
            if extension not in set(EXTENSIONS) | ARCHIVES | DISC_FORMATS | {'.7z'}:
                return 'support', None, 'Executable or emulator resource'
    if name.endswith('.swf') or (any(t in name for t in NON_GAME_PACKAGE_TOKENS)):
        return 'excluded', None, 'Unrelated Flash, flashcart software, BIOS, firmware, or utility'
    hint = segment_hint(p.parts[:-1]) or title_hint(name)
    if extension in ARCHIVES:
        if extension == '.zip':
            return 'archive', hint, 'ZIP contents must be inspected on live filesystem'
        return 'review', hint, '7z/RAR requires separate archive review; not blindly extracted'
    if rel in overrides and overrides[rel] in PLATFORMS:
        hint = overrides[rel]
    if extension in EXTENSIONS:
        # Names with a known and contradictory system are not quietly mislabeled.
        platform = EXTENSIONS[extension]
        if extension in ('.nds', '.3ds') and any(t in name for t in ('boot.', 'installer', 'menu')):
            return 'excluded', None, 'Likely boot/console utility'
        return 'rom', platform, 'Recognized game format'
    if '.nkit.iso' in name or '.nkit.gcz' in name:
        if hint in ('GameCube', 'Wii'):
            if title_hint(name) in ('GameCube', 'Wii'): hint = title_hint(name)
            return 'rom', hint, 'Nintendo NKit image; verify emulator compatibility before launching'
        return 'review', hint, 'NKit format shared by GameCube and Wii'
    if extension in ('.iso', '.gcz', '.rvz', '.ciso', '.chd'):
        if hint in ('GameCube', 'Wii', 'PSP', 'PlayStation', 'PlayStation 2'):
            return 'rom', hint, 'Disc image with console or title context'
        return 'review', hint, 'Disc image used by multiple systems'
    if extension == '.bin' and hint == 'Sega 32X':
        return 'rom', hint, 'Sega 32X .bin in console folder'
    if extension == '.pbp':
        return ('rom', 'PSP', 'PSP package in game folder') if hint == 'PSP' else ('review', hint, 'PBP might be firmware or a game')
    if extension in AMBIGUOUS_IMAGES | {'.cue', '.pbp'}:
        return 'review', hint, 'Ambiguous disc/data file or dependency; grouping required'
    return 'support', None, 'Non-game supporting file'


def safe_archive_member(name: str):
    """Reject ZIP-slip, Windows absolute/drive paths and non-portable path segments."""
    if not name or '\x00' in name or '\\' in name or name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        raise ValueError(f'Unsafe archive member: {name!r}')
    path = PurePosixPath(name)
    if any(seg in ('', '..', '.') or seg.endswith((' ', '.')) or
           any(ch in seg for ch in '<>:"|?*\\') or
           seg.split('.')[0].casefold() in {'con','prn','nul','aux','com1','com2','com3','com4','com5','com6','com7','com8','com9',
                                            'lpt1','lpt2','lpt3','lpt4','lpt5','lpt6','lpt7','lpt8','lpt9'}
           for seg in name.rstrip('/').split('/')):
        raise ValueError(f'Unsafe/nonportable archive member: {name!r}')
    return path.as_posix()


def inspect_zip(source: Path, rel: str, overrides=None):
    """Inspect central directory only; return the members eligible for extraction."""
    try:
        if not zipfile.is_zipfile(source):
            return {'status':'review', 'reason':'Invalid or damaged ZIP signature'}
        with zipfile.ZipFile(source) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return {'status':'review','reason':'Archive has too many entries for safe automatic extraction'}
            all_names = set()
            all_size = 0
            entries = []
            for info in infos:
                safe_name = safe_archive_member(info.filename)
                if info.is_dir(): continue
                if safe_name.casefold() in all_names:
                    raise ValueError('Case-insensitive duplicate member name')
                all_names.add(safe_name.casefold())
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode): raise ValueError('Archive contains symbolic link')
                if info.flag_bits & 0x1: raise ValueError('Encrypted ZIP member')
                all_size += info.file_size
                if (info.file_size > MAX_MEMBER_BYTES or all_size > MAX_ZIP_BYTES or
                    info.file_size > MAX_COMPRESSION_RATIO * max(1, info.compress_size)):
                    raise ValueError('ZIP exceeds extraction size/ratio safety limits')
                entries.append((safe_name, info))
            if not entries: return {'status':'excluded', 'reason':'Empty archive'}
            # Known host-compatible emulator executables identify genuine emulator packages.
            # Preserve every dependency in the ZIP rather than treating files as games.
            emu_exes = [(name, KNOWN_EMULATORS[PurePosixPath(name).name.casefold()])
                        for name,_ in entries if PurePosixPath(name).name.casefold() in KNOWN_EMULATORS]
            if emu_exes:
                names={em[1][0] for em in emu_exes}
                if len(names)!=1 or any(PurePosixPath(n).suffix.casefold() in EXTENSIONS for n,_ in entries):
                    return {'status':'review','reason':'Emulator executable mixed with ROMs or other emulators'}
                return {'status':'emulator','name':next(iter(names)),
                        'reason':'Known PC emulator executable and supporting files inside ZIP',
                        'members':[{'name':n} for n,_ in entries]}
            if any(PurePosixPath(n).suffix.casefold() in BAD_ZIP_FILES for n,_ in entries):
                return {'status':'excluded','reason':'Installer, Flash program, firmware, or software package inside ZIP'}
            if any(PurePosixPath(n).suffix.casefold() in ARCHIVES for n,_ in entries):
                return {'status':'review','reason':'Nested archives require manual inspection'}
            # Every disc dependency needs to travel with its .cue file. CUE/BIN archives
            # without a trusted console hint are held for review.
            hint = (overrides or {}).get(rel) or segment_hint(PurePosixPath(rel).parts[:-1]) or title_hint(PurePosixPath(rel).name)
            cues = [n for n,_ in entries if PurePosixPath(n).suffix.casefold() == '.cue']
            discs = [n for n,_ in entries if PurePosixPath(n).suffix.casefold() in {'.bin','.img'}]
            if cues:
                if hint not in ('PlayStation','PlayStation 2', 'Sega CD', 'Saturn'):
                    return {'status':'review','reason':'CUE and track files require console assignment'}
                for n in cues:
                    info = next(z for fn,z in entries if fn==n)
                    if info.file_size > 1_000_000: return {'status':'review','reason':'Unexpectedly large CUE sheet'}
                    try:
                        contents = zf.read(info).decode('utf-8-sig',errors='replace')
                    except Exception: return {'status':'review','reason':'Cannot read CUE sheet'}
                    references = re.findall(r'(?im)^\s*FILE\s+(?:"([^"]+)"|(\S+))',contents)
                    for pair in references:
                        linked = pair[0] or pair[1]
                        linked_path = str(PurePosixPath(n).parent / linked.replace('\\','/'))
                        if linked_path.casefold() not in all_names:
                            return {'status':'review','reason':'CUE references a track not present inside ZIP'}
                bundle = [n for n,_ in entries if n in cues or n in discs]
                if not discs: return {'status':'review','reason':'CUE archive missing disc tracks'}
                return {'status':'roms','reason':'CUE plus disc tracks; extracted together',
                        'members':[{'name':n,'platform':hint,'group':True} for n in bundle]}
            games = []
            uncertain = []
            for name,info in entries:
                ext=PurePosixPath(name).suffix.casefold()
                if ext in ZIP_HELPERS: continue
                if ext in EXTENSIONS:
                    platform = EXTENSIONS[ext]
                    games.append({'name':name,'platform':platform,'group':False})
                elif ext in DISC_FORMATS | {'.pbp'}:
                    if hint in ('GameCube','Wii','PSP','PlayStation','PlayStation 2') and ext not in {'.bin','.img'}:
                        games.append({'name':name,'platform':hint,'group':False})
                    elif ext == '.bin' and hint == 'Sega 32X':
                        games.append({'name':name,'platform':hint,'group':False})
                    else: uncertain.append(name)
                else:
                    uncertain.append(name)
            if uncertain:
                # Don't silently produce an incomplete game when some files may be needed.
                return {'status':'review','reason':f'Mixed or unknown archive members ({len(uncertain)}); review required'}
            if not games:
                return {'status':'excluded','reason':'Archive contains no recognized game files'}
            return {'status':'roms','reason':f'{len(games)} game files identified by member extensions', 'members':games}
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as e:
        return {'status':'review','reason':f'Cannot safely inspect ZIP: {e}'}


def build_plan(source: str, destination: str, files: list[str], *, inventory_only=False, overrides=None):
    """Build a source-preserving copy/extraction plan. Inventory-only never inspects ZIPs."""
    src = Path(source).expanduser().resolve(strict=not inventory_only)
    dest = Path(destination).expanduser().resolve()
    if dest == src or src in dest.parents or dest in src.parents:
        raise ValueError('Source and destination must be separate, non-nested directories.')
    emulators = discover_emulators(files)
    emdirs = [e['directory'] for e in emulators]
    items,review,pending,excluded = [],[],[],[]
    by_platform = Counter()
    used = set()
    def unique_target(folder, basename, source_key):
        basename = PurePosixPath(basename).name
        target = norm_rel(f'{folder}/{basename}')
        if target.casefold() in used:
            original = PurePosixPath(target)
            tag = sha256(source_key.encode('utf-8')).hexdigest()[:10]
            target = norm_rel(str(original.parent / (original.stem + ' [' + tag + ']' + original.suffix)))
        used.add(target.casefold())
        return target
    for e in emulators:
        source_rel = norm_rel(e['directory'])
        base_name = re.sub(r'[^\w .()+-]+', '_', f"{e['name']} - {PurePosixPath(source_rel).name}")
        target = unique_target('Emulators',base_name,source_rel)
        items.append({'kind':'emulator','source_rel':source_rel,'dest_rel':target,
                      'name':e['name'],'reason':'Preserve complete emulator installation'})
    support_count=0
    zip_success=0
    emulator_zip_success=0
    for rel in files:
        category,platform,reason=classify_file(rel,emdirs,overrides)
        if category in ('support','excluded'):
            support_count+=1
            if category=='excluded': excluded.append({'source_rel':rel,'reason':reason})
        elif category=='review':
            review.append({'source_rel':rel,'platform_hint':platform,'reason':reason})
        elif category=='archive':
            if inventory_only:
                pending.append({'source_rel':rel,'platform_hint':platform,'reason':'ZIP contents unavailable in tree inventory'})
                continue
            found=inspect_zip(src / rel,rel,overrides)
            if found['status']=='review':
                review.append({'source_rel':rel,'platform_hint':platform,'reason':found['reason']})
            elif found['status']=='excluded':
                excluded.append({'source_rel':rel,'reason':found['reason']})
            elif found['status']=='emulator':
                emulator_zip_success+=1
                base_name=re.sub(r'[^\w .()+-]+', '_', found['name']+' - '+PurePosixPath(rel).stem)
                folder=unique_target('Emulators',base_name,rel)
                members=[]
                for m in found['members']:
                    destination_rel=norm_rel(folder+'/'+m['name'])
                    members.append({'name':m['name'],'dest_rel':destination_rel,'platform':None})
                snap=(src/rel).stat()
                items.append({'kind':'archive','archive_type':'emulator','source_rel':rel,'members':members,
                              'size':snap.st_size,'mtime_ns':snap.st_mtime_ns,'reason':found['reason']})
            else:
                mems=[]
                grouped=any(m.get('group') for m in found['members'])
                group_folder = PurePosixPath(rel).stem
                for m in found['members']:
                    console=m['platform']
                    if grouped:
                        # Preserve CUE relative track references, including directories.
                        target_rel = norm_rel(f'ROMs/{console}/{group_folder}/{m["name"]}')
                        if target_rel.casefold() in used:
                            review.append({'source_rel':rel,'platform_hint':console,'reason':'Disc bundle target name collision'})
                            mems=[]
                            break
                        used.add(target_rel.casefold())
                    else:
                        target_rel=unique_target(f'ROMs/{console}', PurePosixPath(m['name']).name,rel+'::'+m['name'])
                    mems.append({'name':m['name'],'dest_rel':target_rel,'platform':console})
                if mems:
                    snapshot=(src/rel).stat()
                    items.append({'kind':'archive','archive_type':'rom','source_rel':rel,'members':mems,
                                  'size':snapshot.st_size, 'mtime_ns':snapshot.st_mtime_ns,
                                  'reason':found['reason']})
                    zip_success+=1
                    for m in mems:
                        if not m['name'].lower().endswith('.cue') and not (grouped and m['name'].lower().endswith(('.bin','.img'))):
                            by_platform[m['platform']]+=1
        elif category=='rom':
            target=unique_target(f'ROMs/{platform}',PurePosixPath(rel).name,rel)
            items.append({'kind':'rom','source_rel':rel,'dest_rel':target,'platform':platform,'reason':reason})
            by_platform[platform]+=1
    return {'format':'romdeck-plan-v2','source':str(src),'destination':str(dest),
            'inventory_only':bool(inventory_only),'items':items,'review':review,
            'pending_archives':pending,'excluded':excluded,'emulators':emulators,
            'summary':{'emulator_installations':len(emulators),'loose_roms':sum(i['kind']=='rom' for i in items),
                       'archives_with_roms':zip_success, 'emulator_zip_packages':emulator_zip_success, 'roms':sum(by_platform.values()),
                       'manual_review':len(review),'pending_archive_inspection':len(pending),
                       'excluded_non_games':len(excluded), 'support_files_left_in_place':support_count,
                       'platforms':dict(by_platform.most_common())}}


def save_plan(plan: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding='utf-8')


def hash_file(path: Path):
    h = sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _safe_target(dest, relative):
    path=dest / norm_rel(relative)
    if path.is_symlink() or not path.resolve().is_relative_to(dest):
        raise ValueError('Unsafe destination path')
    return path


def _apply_archive(origin, item, dest, verify):
    """Stream validated ZIP members to scratch space, then copy without overwriting."""
    if origin.stat().st_size != item['size'] or origin.stat().st_mtime_ns != item['mtime_ns']:
        raise ValueError('Archive changed after plan; regenerate it')
    hint = next((m['platform'] for m in item['members'] if m.get('platform')), None)
    inspection=inspect_zip(origin,item['source_rel'], {item['source_rel']:hint} if hint else None)
    if inspection['status'] != ('emulator' if item.get('archive_type')=='emulator' else 'roms'):
        raise ValueError('ZIP no longer passes content safety checks')
    expected={m['name'] for m in item['members']}
    actual={m['name'] for m in inspection['members']}
    if expected != actual:
        raise ValueError('ZIP contents differ from the planned entries')
    wrote=[]
    with zipfile.ZipFile(origin) as zf, tempfile.TemporaryDirectory(prefix='.romdeck-zip-',dir=dest) as scratch:
        stage=Path(scratch)
        try:
            staged=[]
            for index, member in enumerate(item['members']):
                name=safe_archive_member(member['name'])
                target=_safe_target(dest,member['dest_rel'])
                if target.exists():continue
                info=zf.getinfo(name)
                if info.file_size > MAX_MEMBER_BYTES:raise ValueError('Member exceeds size limit')
                temp=stage/str(index)
                with zf.open(info) as incoming, temp.open('xb') as output:
                    remaining=info.file_size
                    while True:
                        block=incoming.read(min(4*1024*1024,remaining+1))
                        if not block:break
                        remaining-=len(block)
                        if remaining < 0:raise ValueError('Member exceeded declared uncompressed size')
                        output.write(block)
                    if remaining!=0:raise IOError('Truncated ZIP member')
                staged.append((temp,target))
            for temporary,target in staged:
                target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists():continue
                _safe_target(dest,target.relative_to(dest).as_posix())
                try:
                    with temporary.open('rb') as incoming, target.open('xb') as outgoing:
                        shutil.copyfileobj(incoming,outgoing,4*1024*1024)
                    wrote.append(target)
                    if verify and hash_file(target)!=hash_file(temporary):raise IOError('Extracted file SHA-256 mismatch')
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
        except Exception:
            for target in wrote:target.unlink(missing_ok=True)
            raise
    return len(wrote)


def apply_plan(plan: dict, *, verify=False, progress: Callable[[int,int,str],None] | None=None):
    """Copy/stream-extract approved ROMs; no source deletion, overwrite or code execution."""
    if plan.get('format')!='romdeck-plan-v2' or plan.get('inventory_only'):
        raise ValueError('A live romdeck-plan-v2 plan is required. Re-scan original files first.')
    src=Path(plan['source']).expanduser().resolve(strict=True)
    dest=Path(plan['destination']).expanduser().resolve()
    if dest==src or src in dest.parents or dest in src.parents:
        raise ValueError('Unsafe nested source/destination')
    dest.mkdir(parents=True,exist_ok=True)
    counts=Counter()
    errors=[]
    for i,entry in enumerate(plan['items'],1):
        try:
            origin=src/norm_rel(entry['source_rel'])
            if origin.is_symlink() or not origin.resolve().is_relative_to(src):raise ValueError('Source leaves directory')
            if not origin.exists():raise FileNotFoundError(str(origin))
            if entry['kind']=='archive':
                made=_apply_archive(origin,entry,dest,verify)
                counts['emulator_files_extracted' if entry.get('archive_type')=='emulator' else 'archive_rom_files_extracted']+=made
                counts['archives_processed']+=1
                counts['skipped_existing']+=len(entry['members'])-made
                continue
            target=_safe_target(dest,entry['dest_rel'])
            if target.exists():
                counts['skipped_existing']+=1
                continue
            target.parent.mkdir(parents=True,exist_ok=True)
            _safe_target(dest,entry['dest_rel'])
            if entry['kind']=='rom' and origin.is_file():
                try:
                    with origin.open('rb') as incoming, target.open('xb') as outgoing:
                        shutil.copyfileobj(incoming,outgoing,4*1024*1024)
                    shutil.copystat(origin,target)
                    if verify and hash_file(origin)!=hash_file(target):raise IOError('SHA-256 mismatch')
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                counts['roms_copied']+=1
            elif entry['kind']=='emulator' and origin.is_dir():
                if any(p.is_symlink() for p in origin.rglob('*')):
                    raise ValueError('Symlinks inside emulator installation; review manually')
                temporary=target.with_name(target.name+'.romdeck-partial-'+uuid.uuid4().hex[:8])
                try:
                    shutil.copytree(origin,temporary)
                    temporary.rename(target)
                except Exception as exc:
                    raise IOError(f'{exc}; inspect partial copy at {temporary}') from exc
                counts['emulators_copied']+=1
            else:raise ValueError('Source type not eligible for this action')
        except Exception as exc:
            counts['errors']+=1
            errors.append({'source_rel':entry['source_rel'],'error':str(exc)})
        finally:
            if progress:progress(i,len(plan['items']),entry['source_rel'])
    return {'counts':dict(counts),'errors':errors}
