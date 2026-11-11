#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Regression checks for shared cross-platform state and bundled runtime layout."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


import contextlib
import ast
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cryptography.fernet import Fernet
from modules.core import paths, portable
from modules.pro.instance_manager import DatabaseInstance, InstanceManager


def load_file(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortableChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='dbpulse 便携 ')
        self.root=Path(self.temp.name).resolve()
        self.bundle=self.root/'new portable'
        self.bundle.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def layout(self):
        data=self.bundle/'data';data.mkdir(exist_ok=True)
        with patch.multiple(paths,PORTABLE_ROOT=self.bundle,DATA_ROOT=self.bundle,DATA_DIR=data,
                            DB_KEY_PATH=data/'.db_key',DB_KEY_PATH_LEGACY=self.bundle/'.db_key'):
            yield

    def old_installation(self):
        old=self.root/'old Windows app';data=old/'data';data.mkdir(parents=True)
        key=Fernet.generate_key();(data/'.db_key').write_bytes(key)
        secret=Fernet(key).encrypt(b'fixture-database-password').decode()
        db=sqlite3.connect(data/'configuration.db')
        db.execute('CREATE TABLE account(id INTEGER PRIMARY KEY,password TEXT)')
        db.execute('INSERT INTO account VALUES(1,?)',(secret,));db.commit();db.close()
        return old,key

    def test_frozen_nested_binary_discovers_shared_root(self):
        (self.bundle/'dbpulse-portable.json').write_text('{}')
        executable=self.bundle/'bin/windows-x86_64/dbcheck.exe';executable.parent.mkdir(parents=True);executable.write_bytes(b'MZ')
        with patch.dict(os.environ,{},clear=False),patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable',str(executable)):
            os.environ.pop('DBPULSE_PORTABLE_HOME',None)
            configured=load_file('portable_paths_fixture',ROOT/'modules/core/paths.py')
            self.assertEqual(configured.PROJECT_ROOT,self.bundle)
            self.assertEqual(configured.DATA_ROOT,self.bundle)
            self.assertEqual(configured.DB_KEY_PATH,self.bundle/'data/.db_key')

    def test_explicit_shared_root_is_not_executable_directory(self):
        with patch.dict(os.environ,{'DBPULSE_PORTABLE_HOME':str(self.bundle)}):
            configured=load_file('portable_paths_env_fixture',ROOT/'modules/core/paths.py')
            self.assertEqual(configured.INSPECTION_DB,self.bundle/'data/inspection.db')
            self.assertEqual(configured.PRO_DATA_DIR,self.bundle/'data/pro_data')

    def test_windows_report_path_rebases_to_copied_report(self):
        reports=self.bundle/'data/reports';reports.mkdir(parents=True)
        target=reports/'fixture.docx';target.write_bytes(b'fixture')
        with patch.object(paths,'REPORTS_DIR',reports):
            self.assertEqual(paths.resolve_report_file(r'C:\old\DBPulse\data\reports\fixture.docx'),target)
            self.assertEqual(paths.resolve_report_file('/old/linux/data/reports/fixture.docx'),target)
            self.assertFalse(paths.resolve_report_file('/etc/passwd').is_file())

    def test_encrypted_sqlite_and_key_survive_import_and_folder_move(self):
        old,key=self.old_installation()
        result=portable.import_installation(old,self.bundle)
        self.assertTrue(result['credentials_key_preserved'])
        moved=self.root/'Linux copied directory';shutil.copytree(self.bundle,moved)
        self.assertEqual((moved/'data/.db_key').read_bytes(),key)
        with sqlite3.connect(moved/'data/configuration.db') as database:
            encrypted=database.execute('SELECT password FROM account').fetchone()[0]
        self.assertEqual(Fernet((moved/'data/.db_key').read_bytes()).decrypt(encrypted.encode()),b'fixture-database-password')
        self.assertEqual((old/'data/.db_key').read_bytes(),key)

    def test_sqlite_backup_includes_committed_wal(self):
        old,key=self.old_installation()
        database=sqlite3.connect(old/'data/configuration.db')
        database.execute('PRAGMA journal_mode=WAL')
        database.execute('INSERT INTO account VALUES(2,?)',('committed-wal',));database.commit()
        try:portable.import_installation(old,self.bundle)
        finally:database.close()
        self.assertFalse((self.bundle/'data/configuration.db-wal').exists())
        with sqlite3.connect(self.bundle/'data/configuration.db') as copied:
            self.assertEqual(copied.execute('SELECT password FROM account WHERE id=2').fetchone()[0],'committed-wal')

    def test_import_never_overwrites_existing_configuration(self):
        old,_=self.old_installation();data=self.bundle/'data';data.mkdir();(data/'keep').write_text('existing')
        with self.assertRaises(ValueError):portable.import_installation(old,self.bundle)
        self.assertEqual((data/'keep').read_text(),'existing')

    def test_missing_old_key_is_reported_before_creating_new_state(self):
        old,_=self.old_installation();(old/'data/.db_key').unlink()
        with self.assertRaises(ValueError):portable.import_installation(old,self.bundle)
        self.assertFalse((self.bundle/'data').exists())

    def test_external_ssh_and_ca_files_become_portable_references(self):
        external=self.root/'outside key.pem';external.write_text('fixture key content')
        with self.layout():
            relative=portable.preserve_file_reference(str(external),'ssh_key_file')
            self.assertTrue(relative.startswith('data/portable_files/'))
            self.assertEqual((self.bundle/relative).read_text(),'fixture key content')
            self.assertEqual(portable.preserve_file_reference(str(external),'ssh_key_file'),relative)
            self.assertEqual(paths.resolve_portable_file(relative),self.bundle/relative)
            self.assertEqual(external.read_text(),'fixture key content')
            if os.name!='nt':self.assertEqual((self.bundle/relative).stat().st_mode&0o777,0o600)

    def test_instance_passwords_and_key_passphrases_round_trip(self):
        external=self.root/'id_rsa';external.write_text('fixture SSH key')
        with self.layout(),contextlib.redirect_stdout(io.StringIO()):
            manager=InstanceManager(str(self.bundle/'data/pro_data'))
            instance=DatabaseInstance(id='portable-fixture',name='portable',db_type='oracle_jdbc',host='fixture.invalid',
                                      port=1521,user='fixture',password='db-fixture-password',ssh_password='ssh-fixture-password',
                                      ssh_key_password='key-fixture-passphrase',ssh_key_file=str(external))
            self.assertTrue(manager.add_instance(instance)['ok'])
            raw=manager.get_instance('portable-fixture',mask_password=False)
            self.assertNotEqual(raw['password'],'db-fixture-password')
            self.assertFalse(Path(raw['ssh_key_file']).is_absolute())
            decoded=manager.get_all_instances_decrypted()[0]
            self.assertEqual(decoded['password'],'db-fixture-password')
            self.assertEqual(decoded['ssh_password'],'ssh-fixture-password')
            self.assertEqual(decoded['ssh_key_password'],'key-fixture-passphrase')
            self.assertTrue(Path(decoded['ssh_key_file']).is_file())
            self.assertTrue(manager.update_instance('portable-fixture',{'description':'copied to Linux'})['ok'])
            self.assertEqual(manager.get_instance_decrypted('portable-fixture')['password'],'db-fixture-password')

    def test_java_home_is_architecture_specific(self):
        java=self.bundle/'runtime/macos-arm64/jre/bin/java';java.parent.mkdir(parents=True);java.write_text('fixture')
        with patch.object(paths,'PORTABLE_ROOT',self.bundle),patch('modules.core.portable.platform_key',return_value='macos-arm64'):
            self.assertEqual(portable.bundled_java_home(),java.parent.parent)
        with patch.object(paths,'PORTABLE_ROOT',self.bundle),patch('modules.core.portable.platform_key',return_value='linux-x86_64'):
            self.assertIsNone(portable.bundled_java_home())

    def test_saved_datasource_test_reuses_portable_registry_without_reentering_password(self):
        from flask import Flask, jsonify
        from unittest.mock import Mock
        tree=ast.parse((ROOT/'modules/web/app.py').read_text(encoding='utf-8-sig'))
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='api_pro_datasource_test')
        function.decorator_list=[]
        callback=Mock(return_value={'ok':True,'message':'JDBC ready'})
        namespace={'paths':paths,'jsonify':jsonify,'_conn_test_with_timeout':callback}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'saved-datasource-route','exec'),namespace)
        manager=Mock();manager.get_instance_decrypted.return_value={'db_type':'dm','password':'fixture-only'}
        app=Flask(__name__)
        with app.test_request_context(),patch.object(paths,'PORTABLE_ROOT',self.bundle),patch('modules.pro.get_instance_manager',return_value=manager),patch('modules.access.principal_from_session',return_value=object()),patch('modules.access.assert_visible',return_value=(True,None)):
            response=namespace['api_pro_datasource_test']('fixture-id')
            self.assertTrue(response.json['ok'])
            callback.assert_called_once_with('dm',manager.get_instance_decrypted.return_value,'pro')
            self.assertNotIn('fixture-only',response.get_data(as_text=True))
        callback.reset_mock()
        with app.test_request_context(),patch.object(paths,'PORTABLE_ROOT',self.bundle),patch('modules.pro.get_instance_manager',return_value=manager),patch('modules.access.principal_from_session',return_value=object()),patch('modules.access.assert_visible',return_value=(False,{'error':'denied'})):
            response,status=namespace['api_pro_datasource_test']('fixture-id')
            self.assertEqual(status,403)
            callback.assert_not_called()

    def test_windows_arm_host_keeps_x64_process_runtime(self):
        with patch.object(sys,'platform','win32'),patch('platform.machine',return_value='ARM64'),patch('sysconfig.get_platform',return_value='win-amd64'):
            self.assertEqual(portable.platform_key(),'windows-x86_64')
            with patch.object(paths,'PORTABLE_ROOT',self.bundle):
                java=self.bundle/'runtime/windows-x86_64/jre'
                (java/'bin').mkdir(parents=True)
                (java/'bin/java.exe').touch()
                self.assertEqual(portable.bundled_java_home(),java)
        with patch.object(sys,'platform','win32'),patch('platform.machine',return_value='ARM64'),patch('sysconfig.get_platform',return_value='win-arm64'):
            self.assertEqual(portable.platform_key(),'windows-arm64')

    def test_windows_status_launcher_uses_shared_data(self):
        from modules.desktop.windows_launcher import BackendProcess
        (self.bundle/'dbpulse-portable.json').write_text('{}')
        executable=self.bundle/'bin/windows-x86_64'
        executable.mkdir(parents=True)
        with patch.dict(os.environ,{},clear=False):
            os.environ.pop('DBPULSE_PORTABLE_HOME',None)
            controller=BackendProcess(executable)
        self.assertEqual(controller.metadata,self.bundle/'data/desktop_process.json')
        self.assertEqual(controller.executable,executable/'dbcheck.exe')
        launcher=(ROOT/'build/portable/start.bat').read_text()
        self.assertIn('DBPulse.exe',launcher)
        self.assertIn('dbcheck.exe" %*',launcher)

    def test_windows_pe_architecture_is_validated(self):
        builder=load_file('portable_builder_fixture',ROOT/'build/portable_bundle.py')
        path=self.root/'fixture.exe';data=bytearray(512);data[:2]=b'MZ';data[60:64]=(128).to_bytes(4,'little');data[128:132]=b'PE\0\0';data[132:134]=(0x8664).to_bytes(2,'little');path.write_bytes(data)
        builder.validate_binary(path,'windows-x86_64')
        data[132:134]=(0x14c).to_bytes(2,'little');path.write_bytes(data)
        with self.assertRaises(ValueError):builder.validate_binary(path,'windows-x86_64')


if __name__=='__main__':
    unittest.main(verbosity=2)
