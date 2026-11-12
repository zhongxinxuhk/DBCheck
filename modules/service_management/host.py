# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Explicit target-host metadata and log aggregates over verified SSH host keys."""



import hashlib
import re
import stat
from pathlib import PurePosixPath
from .store import utcnow

LOG_KEYS = {'database_alert_log','cluster_alert_log','backup_job_log','target_mail_alerts'}


def collect_host(instance, asset, client_factory=None):
    if not instance.get('ssh_enabled') or asset.get('ssh_host_role') != 'database_host':
        return {}
    import paramiko
    client = (client_factory or paramiko.SSHClient)()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    results = {}
    sftp = None
    try:
        client.connect(hostname=instance['ssh_host'],port=int(instance.get('ssh_port',22)),
                       username=instance['ssh_user'],password=instance.get('ssh_password') or None,
                       key_filename=instance.get('ssh_key_file') or None,
                       passphrase=instance.get('ssh_key_password') or None,
                       timeout=5,banner_timeout=5,auth_timeout=5,look_for_keys=False,allow_agent=False)
        fingerprint = hashlib.sha256(client.get_transport().get_remote_server_key().asbytes()).hexdigest()
        for key,command in {
            'target_filesystems':'LC_ALL=C df -Pk',
            'target_processes':'LC_ALL=C ps -eo pid,stat,comm',
            'target_cpu':'LC_ALL=C vmstat 1 2',
            'target_network_services':'LC_ALL=C netstat -an',
            **({'cluster_nodes':'gcadmin'} if asset.get('db_type')=='gbase8a' else {}),
        }.items():
            try:
                _,stdout,stderr = client.exec_command(command,timeout=8)
                raw = stdout.read(32769)
                error = stderr.read(1024)
                if stdout.channel.recv_exit_status() != 0 or error:
                    raise ValueError('远端命令未成功，需核查操作系统适配或权限')
                results[key] = {'status':'collected','text':raw[:32768].decode('utf-8','replace'),
                                'truncated':len(raw)>32768,'host_key_sha256':fingerprint,
                                'collected_at':utcnow(),'provenance':'verified_target_ssh'}
            except Exception:
                results[key] = {'status':'error','reason':'目标主机指标采集失败'}
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(8)
        hashes = []
        for item in asset.get('core_files',[])[:50]:
            path = item.get('path','')
            if not PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
                raise ValueError('核心文件须使用明确的绝对路径')
            info = sftp.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024*1024:
                hashes.append({'path':path,'status':'blocked','reason':'非普通文件或超过1MB采集预算'})
                continue
            with sftp.open(path,'rb') as remote:
                raw = remote.read(1024*1024+1)
            if len(raw)!=info.st_size:
                hashes.append({'path':path,'status':'blocked','reason':'文件在采集期间变化，须重新采集'})
                continue
            digest = hashlib.sha256(raw).hexdigest()
            expected = item.get('approved_sha256')
            mode_matches = item.get('approved_mode') and stat.S_IMODE(info.st_mode) == int(str(item['approved_mode']),8)
            status = 'pass' if expected==digest and mode_matches else 'fail' if expected and (expected!=digest or item.get('approved_mode') and not mode_matches) else 'needs_review'
            hashes.append({'path':path,'sha256':digest,'permissions':oct(stat.S_IMODE(info.st_mode)),
                           'size':info.st_size,'status':status})
        if hashes:
            results['target_file_hashes'] = {'status':'collected','rows':hashes,'provenance':'verified_target_ssh','collected_at':utcnow()}
            results['target_file_permissions'] = {'status':'collected','rows':[{'path':r['path'],'permissions':r.get('permissions')} for r in hashes], 'provenance':'verified_target_ssh'}
        core_dir = asset.get('core_dump_dir')
        if core_dir:
            from itertools import islice
            if not PurePosixPath(core_dir).is_absolute() or '..' in PurePosixPath(core_dir).parts:
                raise ValueError('核心转储目录须为明确的绝对路径')
            files = list(islice(sftp.listdir_iter(core_dir),201))
            results['target_core_dumps'] = {'status':'collected','count':len(files[:200]),
                    'truncated':len(files)>200,'provenance':'verified_target_ssh','collected_at':utcnow()}
        for key,path in (asset.get('log_paths') or {}).items():
            if key not in LOG_KEYS:
                continue
            if not PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
                raise ValueError('日志路径须为明确的绝对路径')
            info = sftp.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('日志须为普通文件，禁止跟随符号链接')
            with sftp.open(path,'rb') as remote:
                remote.seek(max(0,info.st_size-65536))
                raw = remote.read(65536)
            text = raw.decode('utf-8','replace')
            results[key] = {'status':'collected','file_size':info.st_size,'sample_bytes':len(raw),
                            'error_pattern_count':len(re.findall(r'(?i)error|fatal|panic|corrupt|ORA-\d+',text)),
                            'partial_log':info.st_size>65536,'truncated':info.st_size>65536,'sample_sha256':hashlib.sha256(raw).hexdigest(),
                            'provenance':'verified_target_ssh','collected_at':utcnow()}
    except Exception:
        results['target_host'] = {'status':'error','reason':'目标主机身份、权限、路径或采集失败'}
    finally:
        if sftp is not None:
            sftp.close()
        client.close()
    return results
