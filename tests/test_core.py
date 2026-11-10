import json
import zipfile
import tempfile
import unittest
from pathlib import Path

from romdeck_core import (
    build_plan, from_filesystem, from_inventory, discover_emulators,
    classify_file, apply_plan, inspect_zip, safe_archive_member,
)


class CatalogTests(unittest.TestCase):
    def test_classification(self):
        emulator_dirs=['gamecubeemulator/Dolphin-x64','bsnes_v075-64bit','gba emu']
        cases={
            'vidya/Roms/Cylum Collection/NES/Mario.nes': ('rom','NES'),
            'vidya/Roms/No-Intro/RAW/Nintendo - Game Boy Advance/Game.zip': ('archive','Game Boy Advance'),
            'gamecubeemulator/Fire Emblem - Path of Radiance (USA).iso': ('rom','GameCube'),
            'gamecubeemulator/Fire Emblem - Radiant Dawn (USA) (Rev 1).nkit.iso': ('rom','Wii'),
            'bsnes_v075-64bit/Super Castlevania IV.smc': ('rom','SNES'),
            'gba emu/Pokemon - SoulSilver Version (USA).nds': ('rom','Nintendo DS'),
            'bsnes_v075-64bit/shaders/HLSL/Sepia.shader': ('support',None),
            'ds3/twilightshit/BOOT.NDS': ('support',None),
            'Dolphin Emulator/GC/MemoryCardA.USA.raw': ('support',None),
            'Fire Emblem The Binding Blade.zip': ('archive','Game Boy Advance'),
            'gba_bios.zip': ('excluded',None),
        }
        for fname, expected in cases.items():
            with self.subTest(fname=fname):
                self.assertEqual(classify_file(fname, emulator_dirs)[:2], expected)

    def test_emulator_detection(self):
        files=['gamecubeemulator/Dolphin-x64/Dolphin.exe','gamecubeemulator/Dolphin-x64/DolphinTool.exe',
               'gbe_plus/gbe_plus.exe','gbe_plus/gbe_plus_qt.exe','ds3/Boop.exe']
        emus=discover_emulators(files)
        self.assertEqual(len(emus),2)
        self.assertIn('gbe_plus_qt.exe',next(e['relative_executable'] for e in emus if e['name']=='GBE+'))

    def test_organize_copy_only(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'incoming';target=Path(temp)/'sorted'
            (source/'NES').mkdir(parents=True)
            (source/'bsnes_v075-64bit'/'filters').mkdir(parents=True)
            (source/'NES'/'Game.nes').write_bytes(b'example-game')
            (source/'bsnes_v075-64bit'/'bsnes.exe').write_bytes(b'fake-executable')
            (source/'bsnes_v075-64bit'/'filters'/'some.filter').write_bytes(b'filter')
            (_root, files)=from_filesystem(source)
            plan=build_plan(str(source),str(target),files)
            self.assertEqual(plan['summary']['roms'],1)
            self.assertEqual(plan['summary']['emulator_installations'],1)
            first=apply_plan(plan,verify=True)
            self.assertEqual(first['counts']['roms_copied'],1)
            self.assertEqual(first['counts']['emulators_copied'],1)
            self.assertTrue((source/'NES'/'Game.nes').exists())
            self.assertEqual((target/'ROMs'/'NES'/'Game.nes').read_bytes(),b'example-game')
            first_copy=list((target/'Emulators').rglob('some.filter'))
            self.assertEqual(len(first_copy),1)
            again=apply_plan(plan)
            self.assertEqual(again['counts']['skipped_existing'],2)
            self.assertEqual((source/'NES'/'Game.nes').read_bytes(),b'example-game')

    def test_deny_nested_dest(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source';source.mkdir()
            with self.assertRaises(ValueError):
                build_plan(str(source),str(source/'out'),[])

    def test_inventory_preview_cannot_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source'
            plan=build_plan(str(source),str(Path(temp)/'out'),['Game.gba'],inventory_only=True)
            with self.assertRaises(ValueError): apply_plan(plan)

    def test_existing_destination_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source'; target=Path(temp)/'target'
            (source/'NES').mkdir(parents=True)
            (target/'ROMs'/'NES').mkdir(parents=True)
            (source/'NES'/'Game.nes').write_text('ORIGINAL',encoding='utf-8')
            (target/'ROMs'/'NES'/'Game.nes').write_text('EXISTING',encoding='utf-8')
            root, files=from_filesystem(source)
            plan=build_plan(str(source),str(target),files)
            report=apply_plan(plan)
            self.assertEqual(report['counts']['skipped_existing'],1)
            self.assertEqual((target/'ROMs'/'NES'/'Game.nes').read_text(),'EXISTING')
            self.assertEqual((source/'NES'/'Game.nes').read_text(),'ORIGINAL')

    def test_special_contexts(self):
        e=[]
        self.assertEqual(classify_file('vidya/Roms/Cylum Collection/DS/NDS Pack/Chrono Trigger.zip',e)[:2],('archive','Nintendo DS'))
        self.assertEqual(classify_file('vidya/Roms/Cylum Collection/Sega 32X Collection/Knuckles\' Chaotix.bin',e)[:2],('rom','Sega 32X'))
        self.assertEqual(classify_file('vidya/Roms/Cylum Collection/Famicom Disk System/[BIOS]/DISKSYS.ROM',e)[0],'support')

    def test_collision_preserves_games(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source';source.mkdir()
            files=['NES/a/Game.nes','NES/b/Game.nes']
            p=build_plan(str(source),str(Path(temp)/'dest'),files,inventory_only=True)
            targets=[i['dest_rel'] for i in p['items']]
            self.assertEqual(len(set(targets)),2)

    def test_super_nintendo_not_nes(self):
        self.assertEqual(classify_file('vidya/Roms/No-Intro/RAW/Nintendo - Super Nintendo Entertainment System/game.zip',[])[1], 'SNES')
        self.assertEqual(classify_file('gbe_plus/README.md',[])[0], 'support')
        self.assertEqual(classify_file('ds3/twilightshit/Flashcard users/BOOT.NDS',[])[0], 'support')

    def test_zip_rom_and_collection_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'src'; dest=Path(temp)/'dest'; source.mkdir()
            with zipfile.ZipFile(source/'Games.zip','w') as z:
                z.writestr('Mario.nes',b'NES\x1atest')
                z.writestr('Kirby.gb',b'gameboybytes')
                z.writestr('README.txt','notes')
            plan=build_plan(str(source),str(dest),from_filesystem(source)[1])
            self.assertEqual(plan['summary']['roms'],2)
            self.assertEqual(plan['summary']['archives_with_roms'],1)
            report=apply_plan(plan,verify=True)
            self.assertEqual(report['counts']['archive_rom_files_extracted'],2)
            self.assertEqual((dest/'ROMs'/'NES'/'Mario.nes').read_bytes(),b'NES\x1atest')
            self.assertEqual((dest/'ROMs'/'Game Boy'/'Kirby.gb').read_bytes(),b'gameboybytes')
            self.assertTrue((source/'Games.zip').exists())
            again=apply_plan(plan)
            self.assertEqual(again['counts']['skipped_existing'],2)

    def test_zip_emulator_package(self):
        with tempfile.TemporaryDirectory() as temp:
            src=Path(temp)/'src';dst=Path(temp)/'out';src.mkdir()
            with zipfile.ZipFile(src/'Dolphin.zip','w') as z:
                z.writestr('bundle/Dolphin.exe','not-real')
                z.writestr('bundle/Qt5Core.dll','not-real')
            plan=build_plan(str(src),str(dst),from_filesystem(src)[1])
            self.assertEqual(plan['summary']['emulator_zip_packages'],1)
            report=apply_plan(plan)
            self.assertEqual(report['counts']['emulator_files_extracted'],2)
            self.assertEqual(len(list((dst/'Emulators').rglob('Dolphin.exe'))),1)

    def test_zip_firmware_excluded_and_mixed_requires_review(self):
        with tempfile.TemporaryDirectory() as temp:
            src=Path(temp)/'src';dst=Path(temp)/'out';src.mkdir()
            with zipfile.ZipFile(src/'FlashUtility.zip','w') as z:
                z.writestr('setup.exe','bad')
                z.writestr('game.nes','rom')
            with zipfile.ZipFile(src/'Mixed.zip','w') as z:
                z.writestr('game.nes','rom')
                z.writestr('disc.bin','unknown')
            plan=build_plan(str(src),str(dst),from_filesystem(src)[1])
            self.assertFalse(any(i['kind']=='archive' for i in plan['items']))
            self.assertTrue(any('Mixed.zip' in r['source_rel'] for r in plan['review']))
            self.assertTrue(any('FlashUtility.zip' in r['source_rel'] for r in plan['excluded']))

    def test_zip_slip_and_encryption_protections(self):
        with tempfile.TemporaryDirectory() as temp:
            src=Path(temp)/'src';dst=Path(temp)/'out';src.mkdir()
            with zipfile.ZipFile(src/'evil.zip','w') as z:
                z.writestr('../outside.nes','bad')
            plan=build_plan(str(src),str(dst),from_filesystem(src)[1])
            self.assertEqual(plan['summary']['manual_review'],1)
            self.assertEqual(len(plan['items']),0)
            self.assertFalse((Path(temp)/'outside.nes').exists())

    def test_disc_cue_tracks_together(self):
        with tempfile.TemporaryDirectory() as temp:
            src=Path(temp)/'src'; dst=Path(temp)/'out'; src.mkdir()
            with zipfile.ZipFile(src/'Castlevania - Symphony of the Night (USA).zip','w') as z:
                z.writestr('game.cue','FILE "track01.bin" BINARY\n TRACK 01 MODE2/2352')
                z.writestr('track01.bin',b'fake-track')
            plan=build_plan(str(src),str(dst),from_filesystem(src)[1])
            self.assertEqual(plan['summary']['archives_with_roms'],1)
            report=apply_plan(plan)
            self.assertEqual(report['counts']['archive_rom_files_extracted'],2)
            cues=list((dst/'ROMs'/'PlayStation').rglob('game.cue'))
            self.assertEqual(len(cues),1)
            self.assertTrue((cues[0].parent/'track01.bin').is_file())

    def test_inventory_archives_are_not_trusted(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'src';target=Path(temp)/'target'
            preview=build_plan(str(source),str(target),['NES/a.zip'],inventory_only=True)
            self.assertEqual(preview['summary']['pending_archive_inspection'],1)
            self.assertEqual(preview['summary']['roms'],0)
            with self.assertRaises(ValueError):apply_plan(preview)


if __name__=='__main__': unittest.main()
