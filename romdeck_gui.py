#!/usr/bin/env python3
"""RomDeck: no-dependency Windows/Linux ROM library and emulator launcher.

Windows-first. Requires Python 3.10+ with Tkinter. Everything stays on-device.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path, PurePosixPath
from tkinter import ttk, filedialog, messagebox, simpledialog

from romdeck_core import (
    PLATFORMS, from_filesystem, discover_emulators, classify_file,
    build_plan, save_plan, apply_plan,
)


def state_dir():
    override = os.getenv('ROMDECK_CONFIG_DIR')
    if override:
        return Path(override).expanduser()
    if sys.platform == 'win32':
        base = Path(os.getenv('LOCALAPPDATA') or Path.home())
        return base / 'RomDeck'
    return Path(os.getenv('XDG_CONFIG_HOME') or Path.home()/'.config') / 'romdeck'


STATE_DIR = state_dir()
CONFIG_FILE = STATE_DIR / 'settings.json'


def load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        if isinstance(data, dict): return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_config(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(CONFIG_FILE)


def display_title(rel):
    name = PurePosixPath(rel).name
    for ending in ('.nkit.iso', '.nkit.gcz', '.zip', '.7z', '.rar', '.iso', '.gcz', '.rvz'):
        if name.lower().endswith(ending): return name[:-len(ending)]
    return PurePosixPath(name).stem


class RomDeck(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('RomDeck — ROM & Emulator Manager')
        self.geometry('1180x770')
        self.minsize(860, 540)
        self.config_data = load_config()
        self.source_var = tk.StringVar(value=self.config_data.get('source', ''))
        self.dest_var = tk.StringVar(value=self.config_data.get('destination', ''))
        self.search_var = tk.StringVar()
        self.platform_filter = tk.StringVar(value='All consoles')
        self.selected_emu_var = tk.StringVar()
        self.status_var = tk.StringVar(value='Choose your collection directory, then Scan.')
        self.progress_q = queue.Queue()
        self.files = []
        self.games = []
        self.review = []
        self.emulators = []
        self.emulator_choice_map = {}
        self.game_rows = {}
        self.review_rows = {}
        self.plan = None
        self._make_ui()
        self.after(100, self._poll_progress)
        if self.source_var.get() and Path(self.source_var.get()).is_dir():
            self.after(350, self.scan)

    def _make_ui(self):
        style = ttk.Style(self)
        if 'clam' in style.theme_names(): style.theme_use('clam')
        style.configure('Treeview', rowheight=26)
        style.configure('TButton', padding=(8, 6))
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill='both', expand=True)
        top = ttk.LabelFrame(outer, text='Collection', padding=8)
        top.pack(fill='x')
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text='Source folder:').grid(row=0, column=0, padx=5)
        ttk.Entry(top, textvariable=self.source_var).grid(row=0, column=1, sticky='ew', padx=5)
        ttk.Button(top, text='Browse…', command=self.choose_root).grid(row=0, column=2, padx=4)
        ttk.Button(top, text='Scan library', command=self.scan).grid(row=0, column=3, padx=4)
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill='both', expand=True, pady=(12, 6))
        self.games_tab = ttk.Frame(self.tabs, padding=10)
        self.emus_tab = ttk.Frame(self.tabs, padding=10)
        self.review_tab = ttk.Frame(self.tabs, padding=10)
        self.organize_tab = ttk.Frame(self.tabs, padding=10)
        for frame, title in [(self.games_tab, 'Games'), (self.emus_tab, 'Emulators'),
                             (self.review_tab, 'Needs Review'), (self.organize_tab, 'Organizer')]:
            self.tabs.add(frame, text=title)
        self._build_games()
        self._build_emulators()
        self._build_review()
        self._build_organizer()
        ttk.Label(outer, textvariable=self.status_var, anchor='w').pack(fill='x', pady=(2, 0))

    def _build_games(self):
        frm = self.games_tab
        controls = ttk.Frame(frm)
        controls.pack(fill='x', pady=(0,8))
        ttk.Label(controls, text='Search:').pack(side='left', padx=(0,6))
        ttk.Entry(controls, textvariable=self.search_var, width=32).pack(side='left', padx=(0,12))
        ttk.Label(controls, text='Console:').pack(side='left')
        ttk.Combobox(controls, textvariable=self.platform_filter, state='readonly',
                     width=22, values=('All consoles',)+PLATFORMS).pack(side='left', padx=6)
        self.search_var.trace_add('write', lambda *_: self.refresh_games())
        self.platform_filter.trace_add('write', lambda *_: self.refresh_games())
        tablefrm = ttk.Frame(frm)
        tablefrm.pack(fill='both', expand=True)
        self.game_table = ttk.Treeview(tablefrm, columns=('title','platform','path'), show='headings', selectmode='browse')
        for key, label, width in [('title','Game',270),('platform','Console',175),('path','Original path',650)]:
            self.game_table.heading(key, text=label)
            self.game_table.column(key, width=width, stretch=key!='platform')
        self.game_table.pack(side='left', fill='both', expand=True)
        game_scroll=ttk.Scrollbar(tablefrm, orient='vertical', command=self.game_table.yview)
        game_scroll.pack(side='right', fill='y')
        self.game_table.configure(yscrollcommand=game_scroll.set)
        self.game_table.bind('<<TreeviewSelect>>', lambda e: self._game_selected())
        bottom = ttk.Frame(frm)
        bottom.pack(fill='x', pady=(10,0))
        ttk.Label(bottom, text='Use emulator:').pack(side='left', padx=5)
        self.emulator_combo = ttk.Combobox(bottom, textvariable=self.selected_emu_var,
                                            state='readonly', width=46)
        self.emulator_combo.pack(side='left', padx=6)
        ttk.Button(bottom, text='Play selected game', command=self.launch_selected_game).pack(side='left', padx=7)
        ttk.Button(bottom, text='Configure arguments', command=self.configure_selected_emulator).pack(side='left', padx=5)

    def _build_emulators(self):
        frm=self.emus_tab
        ttk.Label(frm, text='Detected executable installations. Windows .exe files cannot be launched directly on Linux.').pack(anchor='w', pady=(0,10))
        self.emu_table = ttk.Treeview(frm, columns=('name','platforms','executable'), show='headings', selectmode='browse')
        for k,t,w in [('name','Emulator',160),('platforms','Consoles',290),('executable','Executable path',650)]:
            self.emu_table.heading(k, text=t)
            self.emu_table.column(k, width=w)
        self.emu_table.pack(fill='both', expand=True)
        btns=ttk.Frame(frm)
        btns.pack(fill='x', pady=(10,0))
        ttk.Button(btns, text='Add installed emulator…', command=self.add_emulator).pack(side='left', padx=4)
        ttk.Button(btns, text='Launch emulator', command=self.launch_emulator_only).pack(side='left', padx=4)
        ttk.Button(btns, text='Edit launch arguments', command=self.configure_emulator_tab).pack(side='left', padx=4)

    def _build_review(self):
        frm=self.review_tab
        ttk.Label(frm, text='Unknown disc images, 7z/RAR archives and mixed/unsafe ZIPs are not automatically extracted. Review manually.').pack(anchor='w', pady=(0,8))
        self.review_table = ttk.Treeview(frm, columns=('file','hint','reason'), show='headings', selectmode='browse')
        for k,t,w in [('file','Relative path',650),('hint','Suggested',125),('reason','Reason',325)]:
            self.review_table.heading(k, text=t)
            self.review_table.column(k, width=w)
        self.review_table.pack(fill='both', expand=True)
        ttk.Button(frm, text='Assign platform to selected file…', command=self.assign_platform).pack(anchor='w', pady=(10,0))

    def _build_organizer(self):
        frm=self.organize_tab
        ttk.Label(frm, text='Non-destructive organizer: it creates an organized COPY while leaving your original library untouched.').pack(anchor='w', pady=7)
        row=ttk.Frame(frm)
        row.pack(fill='x', pady=8)
        row.columnconfigure(1,weight=1)
        ttk.Label(row, text='Destination folder:').grid(row=0,column=0,padx=5)
        ttk.Entry(row, textvariable=self.dest_var).grid(row=0,column=1,sticky='ew',padx=5)
        ttk.Button(row, text='Browse…', command=self.choose_dest).grid(row=0,column=2,padx=5)
        btns=ttk.Frame(frm)
        btns.pack(fill='x',pady=6)
        ttk.Button(btns, text='1. Create preview plan', command=self.preview_organization).pack(side='left', padx=6)
        self.copy_button=ttk.Button(btns, text='2. Copy and extract approved games/emulators', command=self.copy_collection, state='disabled')
        self.copy_button.pack(side='left', padx=6)
        self.organize_output=tk.Text(frm, height=20, font=('Consolas' if sys.platform=='win32' else 'monospace',10), wrap='word')
        self.organize_output.pack(fill='both',expand=True,pady=8)
        self.organize_output.insert('1.0', 'Run a scan, choose a destination outside the source directory, then preview.\n\n'
             'The organizer will copy recognized games under ROMs/<console>/, preserve complete emulator installations under Emulators/,\n'
             'and safely extract confirmed ZIP game contents while leaving firmware, Flash/flashcart software, saves, and ambiguous archives untouched.\n')
        self.organize_output.configure(state='disabled')
        self.progress=ttk.Progressbar(frm,mode='determinate')
        self.progress.pack(fill='x')

    def choose_root(self):
        folder=filedialog.askdirectory(title='Choose the directory containing your games and emulators')
        if folder:
            self.source_var.set(folder)
            self.plan=None
            self.copy_button.configure(state='disabled')

    def choose_dest(self):
        folder=filedialog.askdirectory(title='Choose a SEPARATE destination directory')
        if folder:
            self.dest_var.set(folder)
            self.plan=None
            self.copy_button.configure(state='disabled')

    def _save(self):
        self.config_data['source'] = self.source_var.get().strip()
        self.config_data['destination'] = self.dest_var.get().strip()
        save_config(self.config_data)

    def scan(self):
        source=Path(self.source_var.get()).expanduser()
        if not source.is_dir():
            messagebox.showerror('Missing directory','Choose an existing folder containing your games and emulators.')
            return
        try:
            _root,self.files=from_filesystem(source)
            self.emulators=discover_emulators(self.files)
            for e in self.emulators:
                e['executable']=str(source / e['relative_executable'])
            self.emulators.extend(self.config_data.get('manual_emulators',[]))
            emdirs=[e['directory'] for e in self.emulators if 'directory' in e]
            overrides=self.config_data.get('overrides',{})
            self.games=[]; self.review=[]; pending_archives=0
            for rel in self.files:
                kind,platform,reason=classify_file(rel,emdirs,overrides)
                if kind=='rom': self.games.append({'rel':rel,'platform':platform,'reason':reason})
                elif kind=='review': self.review.append({'rel':rel,'hint':platform or '', 'reason':reason})
                elif kind=='archive': pending_archives+=1
            self.games.sort(key=lambda g:(g['platform'],display_title(g['rel']).casefold()))
            self.refresh_games()
            self.refresh_emulators()
            self.refresh_review()
            self.plan=None; self.copy_button.configure(state='disabled')
            self._save()
            self.status_var.set(f'{len(self.games):,} loose ROMs, {pending_archives:,} ZIPs awaiting content scan, {len(self.emulators)} emulators, {len(self.review)} review items.')
        except Exception as exc:
            messagebox.showerror('Scan failed',str(exc))

    def refresh_games(self):
        self.game_rows={}
        if not hasattr(self,'game_table'): return
        self.game_table.delete(*self.game_table.get_children())
        term=self.search_var.get().casefold().strip()
        console=self.platform_filter.get()
        count=0
        for g in self.games:
            if console!='All consoles' and g['platform']!=console: continue
            if term and term not in (display_title(g['rel'])+' '+g['platform']+' '+g['rel']).casefold(): continue
            row=self.game_table.insert('', 'end', values=(display_title(g['rel']),g['platform'],g['rel']))
            self.game_rows[row]=g
            count+=1
        self.status_var.set(f'Showing {count:,} of {len(self.games):,} games.')

    def refresh_emulators(self):
        self.emu_table.delete(*self.emu_table.get_children())
        self.emu_rows={}
        for e in self.emulators:
            row=self.emu_table.insert('', 'end',values=(e['name'],', '.join(e['platforms']),e['executable']))
            self.emu_rows[row]=e

    def refresh_review(self):
        self.review_table.delete(*self.review_table.get_children())
        self.review_rows={}
        for r in self.review:
            row=self.review_table.insert('','end',values=(r['rel'],r['hint'],r['reason']))
            self.review_rows[row]=r

    def _selected_game(self):
        sel=self.game_table.selection()
        return self.game_rows.get(sel[0]) if sel else None

    def _game_selected(self):
        g=self._selected_game()
        if not g: return
        self.emulator_choice_map={}
        for e in self.emulators:
            if g['platform'] in e['platforms']:
                label=f"{e['name']}  |  {e['executable']}"
                self.emulator_choice_map[label]=e
        options=list(self.emulator_choice_map)
        self.emulator_combo.configure(values=options)
        preferred=self.config_data.get('default_emulators',{}).get(g['platform'])
        choice=next((key for key,e in self.emulator_choice_map.items() if e['executable']==preferred),None)
        self.selected_emu_var.set(choice or (options[0] if options else ''))
        if not options:
            self.status_var.set(f'No registered emulator for {g["platform"]}. Add one in the Emulators tab.')

    def _selected_emulator(self):
        return self.emulator_choice_map.get(self.selected_emu_var.get())

    def _selected_emu_row(self):
        sel=self.emu_table.selection()
        return self.emu_rows.get(sel[0]) if sel else None

    def add_emulator(self):
        chosen=filedialog.askopenfilename(title='Select installed emulator executable')
        if not chosen: return
        name=simpledialog.askstring('Emulator name','Display name (e.g. PPSSPP):',parent=self)
        if not name:return
        choices=', '.join(PLATFORMS)
        platform_text=simpledialog.askstring('Supported consoles',
            'Comma-separated console names (must match one or more from the README):\n\n' + choices,
            parent=self)
        if not platform_text:return
        platforms=[x.strip() for x in platform_text.split(',') if x.strip() in PLATFORMS]
        if not platforms:
            messagebox.showerror('No valid consoles', 'No console names matched. Try Game Boy Advance, PSP, SNES, etc.')
            return
        args=simpledialog.askstring('Launch arguments',
             'CLI arguments to launch a game; use {rom} for its full path.\nLeave blank to launch emulator only:',
             initialvalue='{rom}', parent=self)
        if args is None:return
        record={'name':name,'platforms':platforms,'executable':chosen,'arguments':args}
        self.config_data.setdefault('manual_emulators',[]).append(record)
        self._save(); self.scan()

    def _args_for(self,e):
        return self.config_data.get('argument_overrides',{}).get(e['executable'],e.get('arguments',''))

    def _config_args(self,e):
        if not e:
            messagebox.showinfo('Select emulator', 'Select an emulator first.')
            return
        args=simpledialog.askstring('Arguments for '+e['name'],
            'Arguments sent when launching a game. Use {rom} for the full file path.\n'
            'Example: -b -e {rom} for Dolphin. Leave empty to launch only the emulator.',
            initialvalue=self._args_for(e),parent=self)
        if args is not None:
            self.config_data.setdefault('argument_overrides',{})[e['executable']]=args
            self._save()

    def configure_selected_emulator(self): self._config_args(self._selected_emulator())
    def configure_emulator_tab(self): self._config_args(self._selected_emu_row())

    def _launch(self,e,rom=None):
        if not e:
            messagebox.showwarning('No emulator','Select or register an emulator first.')
            return
        exe=Path(e['executable'])
        if not exe.is_file():
            messagebox.showerror('Missing executable',f'Cannot find:\n{exe}')
            return
        if sys.platform!='win32' and exe.suffix.lower()=='.exe':
            messagebox.showwarning('Windows emulator on Linux',
                'This is a Windows .exe. RomDeck does not run Wine automatically.\n'
                'Register a native Linux emulator from the Emulators tab, or run on Windows.')
            return
        if sys.platform!='win32' and not os.access(exe,os.X_OK):
            messagebox.showerror('Not executable',f'Executable bit is not set:\n{exe}')
            return
        cmd=[str(exe)]
        if rom:
            template=self._args_for(e)
            if '{rom}' not in template:
                messagebox.showinfo('Arguments needed',
                     f'No ROM argument has been configured for {e["name"]}.\n'
                     'Click Configure arguments and include {rom}, or launch the emulator alone.')
                return
            try:
                tokens=shlex.split(template)
            except ValueError as exc:
                messagebox.showerror('Invalid arguments',str(exc));return
            cmd.extend(x.replace('{rom}',str(rom)).replace('{emu_dir}',str(exe.parent)) for x in tokens)
        try:
            subprocess.Popen(cmd,cwd=str(exe.parent), shell=False)
            self.status_var.set(f'Launched {e["name"]}' + (f' with {Path(rom).name}' if rom else ''))
        except OSError as exc:
            messagebox.showerror('Unable to launch',str(exc))

    def launch_selected_game(self):
        g=self._selected_game(); e=self._selected_emulator()
        if not g:
            messagebox.showinfo('Select a game','Select a game first.');return
        if not e:
            messagebox.showinfo('No compatible emulator',
                 'Register an emulator that supports '+g['platform']+' in the Emulators tab.');return
        rom=Path(self.source_var.get())/g['rel']
        if not rom.is_file():
            messagebox.showerror('File unavailable',f'Cannot find {rom}');return
        if rom.suffix.lower() in ('.zip','.7z','.rar'):
            if not messagebox.askokcancel('Archive compatibility',
                   'This game is an archive. Not every emulator can load compressed files directly.\n'
                   'RomDeck will pass the archive path to the emulator without extracting it. Continue?'):
                return
        self.config_data.setdefault('default_emulators',{})[g['platform']]=e['executable']
        self._save()
        self._launch(e,rom)

    def launch_emulator_only(self): self._launch(self._selected_emu_row())

    def assign_platform(self):
        sel=self.review_table.selection()
        r=self.review_rows.get(sel[0]) if sel else None
        if not r:
            messagebox.showinfo('Select a file','Select a file from the review list.');return
        if PurePosixPath(r['rel']).suffix.lower() in ('.7z','.rar'):
            messagebox.showinfo('Manual archive inspection','7z/RAR files cannot be automatically extracted by this version. Extract with a trusted local archive tool and re-scan.')
            return
        popup=tk.Toplevel(self)
        popup.title('Assign platform')
        popup.geometry('580x165')
        ttk.Label(popup,text=r['rel'],wraplength=550).pack(padx=12,pady=10)
        new=tk.StringVar(value=r['hint'] if r['hint'] in PLATFORMS else PLATFORMS[0])
        ttk.Combobox(popup,textvariable=new,values=PLATFORMS,state='readonly',width=28).pack()
        def save():
            self.config_data.setdefault('overrides',{})[r['rel']]=new.get()
            self._save();popup.destroy();self.scan()
        ttk.Button(popup,text='Assign and rescan',command=save).pack(pady=10)
        popup.transient(self)
        popup.grab_set()

    def _print_organize(self,message):
        self.organize_output.configure(state='normal')
        self.organize_output.delete('1.0','end')
        self.organize_output.insert('1.0',message)
        self.organize_output.configure(state='disabled')

    def preview_organization(self):
        source=self.source_var.get().strip()
        dest=self.dest_var.get().strip()
        if not source or not dest:
            messagebox.showerror('Paths needed','Choose both source and destination directories.');return
        self.plan=None
        self.copy_button.configure(state='disabled')
        self.status_var.set('Inspecting ZIP contents locally. No source files will be changed.')
        self._print_organize('Scanning the source collection and examining ZIP central directories.\n'
                             'Only recognized ROM contents will be planned for extraction.\n'
                             'No files have been changed.')
        overrides=dict(self.config_data.get('overrides',{}))
        def worker():
            try:
                _r,files=from_filesystem(Path(source))
                plan=build_plan(source,dest,files,overrides=overrides)
                self.progress_q.put(('planned',plan))
            except Exception as exc:
                self.progress_q.put(('error',str(exc)))
        threading.Thread(target=worker,daemon=True).start()

    def _show_plan(self,plan):
        self.plan=plan
        self.review=[{'rel':r['source_rel'],'hint':r.get('platform_hint') or '', 'reason':r['reason']} for r in plan['review']]
        self.refresh_review()
        STATE_DIR.mkdir(parents=True,exist_ok=True)
        save_plan(plan,STATE_DIR/'last_plan.json')
        summary=plan['summary']
        lines=[f"Source: {plan['source']}", f"Destination: {plan['destination']}",
               '', f"ROMs proposed (including ZIP members): {summary['roms']:,}",
               f"Loose ROMs: {summary['loose_roms']:,}",
               f"ZIP archives containing ROMs: {summary['archives_with_roms']:,}",
               f"ZIP archives containing PC emulator packages: {summary['emulator_zip_packages']:,}",
               f"Installed emulator folders: {summary['emulator_installations']:,}",
               f"Needs manual review: {summary['manual_review']:,}",
               f"Excluded non-games: {summary['excluded_non_games']:,}",
               '', 'Consoles:']
        lines.extend(f'  {name}: {n:,}' for name,n in summary['platforms'].items())
        lines.extend(['', f"Review plan: {STATE_DIR/'last_plan.json'}",
             'Originals will remain unchanged. No extraction has occurred yet.'])
        self._print_organize('\n'.join(lines))
        self.copy_button.configure(state='normal')
        self._save()
        self.status_var.set('Archive inspection complete; review the plan before copying.')

    def copy_collection(self):
        if not self.plan:return
        if not messagebox.askyesno('Copy to organized library',
              f'Copy {len(self.plan["items"]):,} items to:\n{self.plan["destination"]}\n\n'
              'Verified ZIP game contents will be extracted into the new library. This may need significant disk space. Existing files will be skipped. '
              'The original collection will NOT be changed. Proceed?'):
            return
        self.copy_button.configure(state='disabled')
        self.progress['maximum']=len(self.plan['items'])
        self.progress['value']=0
        plan=self.plan
        def worker():
            try:
                report=apply_plan(plan,progress=lambda i,n,p: self.progress_q.put(('progress',i,n,p)))
                report_path=STATE_DIR/'last_copy_report.json'
                report_path.write_text(json.dumps(report,indent=2),encoding='utf-8')
                self.progress_q.put(('done',report,report_path))
            except Exception as exc:
                self.progress_q.put(('error',str(exc)))
        threading.Thread(target=worker,daemon=True).start()
        self.status_var.set('Copying and extracting locally. Original library remains unchanged.')

    def _poll_progress(self):
        try:
            for _ in range(100):
                event=self.progress_q.get_nowait()
                if event[0]=='progress':
                    _,i,n,rel=event
                    self.progress['value']=i
                    self.status_var.set(f'Copying {i:,}/{n:,}: {PurePosixPath(rel).name}')
                elif event[0]=='planned':
                    self._show_plan(event[1])
                elif event[0]=='done':
                    _,report,path=event
                    self._print_organize(json.dumps(report['counts'],indent=2)+'\n\nFull report: '+str(path))
                    self.status_var.set('Copy operation finished. Source collection untouched.')
                    messagebox.showinfo('Copy complete',f"Results: {report['counts']}\n\nDetails: {path}")
                    self.copy_button.configure(state='normal')
                elif event[0]=='error':
                    messagebox.showerror('Copy failed',event[1]);self.copy_button.configure(state='normal')
        except queue.Empty:pass
        self.after(100,self._poll_progress)


if __name__=='__main__':
    RomDeck().mainloop()
