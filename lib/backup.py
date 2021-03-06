import boto
import copy
import datetime
import os
import prctl
import psutil
import re
import resource
import signal
import subprocess
import time
import urllib

import safe_uploader
import mysql_lib
import host_utils
from lib import environment_specific


BACKUP_FILE = ('{backup_type}/{retention_policy}/{replica_set}/'
               '{hostname}-{port}-{timestamp}.{extension}')
BACKUP_FILE_INITIAL = ('{backup_type}/initial_build/'
                       '{hostname}-{port}-{timestamp}.{extension}')
BACKUP_SEARCH_PREFIX = ('{backup_type}/{retention_policy}/{replica_set}/'
                        '{hostname}-{port}-{timestamp}')
BACKUP_SEARCH_INITIAL_PREFIX = ('{backup_type}/initial_build/'
                                '{hostname}-{port}-{timestamp}')
BACKUP_LOCK_FILE = '/tmp/backup_mysql.lock'
BACKUP_TYPE_LOGICAL = 'mysqldump'
BACKUP_TYPE_LOGICAL_EXTENSION = 'sql.gz'
BACKUP_TYPE_CSV = 'csv'
BACKUP_TYPE_XBSTREAM = 'xtrabackup'
BACKUP_TYPE_XBSTREAM_EXTENSION = 'xbstream'
BACKUP_TYPES = set([BACKUP_TYPE_LOGICAL, BACKUP_TYPE_XBSTREAM,
                    BACKUP_TYPE_CSV])
DEFAULT_MAX_RESTORE_AGE = 5
INNOBACKUP_DECOMPRESS_THREADS = 8
INNOBACKUPEX = '/usr/bin/innobackupex'
INNOBACKUP_OK = 'completed OK!'
NO_BACKUP = 'Unable to find a valid backup for '
MYSQLDUMP = '/usr/bin/mysqldump'
MYSQLDUMP_CMD = ' '.join((MYSQLDUMP,
                          '--master-data',
                          '--single-transaction',
                          '--events',
                          '--all-databases',
                          '--routines',
                          '--user={dump_user}',
                          '--password={dump_pass}',
                          '--host={host}',
                          '--port={port}'))
PIGZ = ['/usr/bin/pigz', '-p', '8']
PV = ['/usr/bin/pv', '-peafbt']
S3_SCRIPT = '/usr/local/bin/gof3r'
USER_ROLE_MYSQLDUMP = 'mysqldump'
USER_ROLE_XTRABACKUP = 'xtrabackup'
XB_RESTORE_STATUS = ("CREATE TABLE IF NOT EXISTS test.xb_restore_status ("
                     "id                INT UNSIGNED NOT NULL AUTO_INCREMENT, "
                     "restore_source    VARCHAR(64), "
                     "restore_type      ENUM('s3', 'remote_server', "
                     "                       'local_file') NOT NULL, "
                     "test_restore      ENUM('normal', 'test') NOT NULL, "
                     "restore_destination   VARCHAR(64), "
                     "restore_date      DATE, "
                     "restore_port      SMALLINT UNSIGNED NOT NULL "
                     "                  DEFAULT 3306, "
                     "restore_file      VARCHAR(255), "
                     "replication       ENUM('SKIP', 'REQ', 'OK', 'FAIL'), "
                     "zookeeper         ENUM('SKIP', 'REQ', 'OK', 'FAIL'), "
                     "started_at        DATETIME NOT NULL, "
                     "finished_at       DATETIME, "
                     "restore_status    ENUM('OK', 'IPR', 'BAD') "
                     "                  DEFAULT 'IPR', "
                     "status_message    TEXT, "
                     "PRIMARY KEY(id), "
                     "INDEX (restore_type, started_at), "
                     "INDEX (restore_type, restore_status, "
                     "       started_at) )")

XBSTREAM = ['/usr/bin/xbstream', '--extract']
XTRABACKUP_CMD = ' '.join((INNOBACKUPEX,
                           '--defaults-file={cnf}',
                           '--defaults-group={cnf_group}',
                           '--slave-info',
                           '--safe-slave-backup',
                           '--parallel=8',
                           '--stream=xbstream',
                           '--no-timestamp',
                           '--compress',
                           '--compress-threads=8',
                           '--kill-long-queries-timeout=10',
                           '--user={xtra_user}',
                           '--password={xtra_pass}',
                           '--port={port}',
                           '{datadir}'))
MINIMUM_VALID_BACKUP_SIZE_BYTES = 1024 * 1024

log = environment_specific.setup_logging_defaults(__name__)


def create_backup_file_name(instance, timestamp, initial_build, backup_type):
    """ Figure out where to put a backup in s3

    Args:
    instance - A hostaddr instance
    timestamp - A timestamp which will be used to create the backup filename
    initial_build - Boolean, if this is being created right after the server
                    was built
    backup_type - xtrabackup or mysqldump

    Returns:
    A string of the path to the finished backup
    """
    timestamp_formatted = time.strftime('%Y-%m-%d-%H:%M:%S', timestamp)
    if backup_type == BACKUP_TYPE_LOGICAL:
        extension = BACKUP_TYPE_LOGICAL_EXTENSION
    elif backup_type == BACKUP_TYPE_XBSTREAM:
        extension = BACKUP_TYPE_XBSTREAM_EXTENSION
    else:
        raise Exception('Unsupported backup type {}'.format(backup_type))

    if initial_build:
        return BACKUP_FILE_INITIAL.format(
            backup_type=backup_type,
            hostname=instance.hostname,
            port=instance.port,
            timestamp=timestamp_formatted,
            extension=extension)
    else:
        return BACKUP_FILE.format(
             retention_policy=environment_specific.get_backup_retention_policy(instance),
             backup_type=backup_type,
             replica_set=instance.get_zk_replica_set()[0],
             hostname=instance.hostname,
             port=instance.port,
             timestamp=timestamp_formatted,
             extension=extension)


def logical_backup_instance(instance, timestamp, initial_build):
    """ Take a compressed mysqldump backup

    Args:
    instance - A hostaddr instance
    timestamp - A timestamp which will be used to create the backup filename
    initial_build - Boolean, if this is being created right after the server
                    was built

    Returns:
    A string of the path to the finished backup
    """
    backup_file = create_backup_file_name(instance, timestamp,
                                          initial_build,
                                          BACKUP_TYPE_LOGICAL)
    (dump_user,
     dump_pass) = mysql_lib.get_mysql_user_for_role(USER_ROLE_MYSQLDUMP)
    dump_cmd = MYSQLDUMP_CMD.format(dump_user=dump_user,
                                    dump_pass=dump_pass,
                                    host=instance.hostname,
                                    port=instance.port).split()

    procs = dict()
    try:
        log.info(' '.join(dump_cmd + ['|']))
        procs['mysqldump'] = subprocess.Popen(dump_cmd,
                                              stdout=subprocess.PIPE)
        procs['pv'] = create_pv_proc(procs['mysqldump'].stdout)
        log.info(' '.join(PIGZ + ['|']))
        procs['pigz'] = subprocess.Popen(PIGZ,
                                         stdin=procs['pv'].stdout,
                                         stdout=subprocess.PIPE)
        log.info('Uploading backup to s3://{buk}/{key}'
                 ''.format(buk=environment_specific.BACKUP_BUCKET_UPLOAD_MAP[host_utils.get_iam_role()],
                           key=backup_file))
        safe_uploader.safe_upload(precursor_procs=procs,
                                  stdin=procs['pigz'].stdout,
                                  bucket=environment_specific.BACKUP_BUCKET_UPLOAD_MAP[host_utils.get_iam_role()],
                                  key=backup_file)
        log.info('mysqldump was successful')
        return backup_file
    except:
        safe_uploader.kill_precursor_procs(procs)
        raise


def xtrabackup_instance(instance, timestamp, initial_build):
    """ Take a compressed mysql backup

    Args:
    instance - A hostaddr instance
    timestamp - A timestamp which will be used to create the backup filename
    initial_build - Boolean, if this is being created right after the server
                    was built

    Returns:
    A string of the path to the finished backup
    """
    # Prevent issues with too many open files
    resource.setrlimit(resource.RLIMIT_NOFILE, (131072, 131072))
    backup_file = create_backup_file_name(instance, timestamp,
                                          initial_build,
                                          BACKUP_TYPE_XBSTREAM)

    tmp_log = os.path.join(environment_specific.RAID_MOUNT,
                           'log', 'xtrabackup_{ts}.log'.format(
                            ts=time.strftime('%Y-%m-%d-%H:%M:%S', timestamp)))
    tmp_log_handle = open(tmp_log, "w")
    procs = dict()
    try:
        cmd = create_xtrabackup_command(instance, timestamp, tmp_log)
        log.info(' '.join(cmd + [' 2> ', tmp_log, ' | ']))
        procs['xtrabackup'] = subprocess.Popen(cmd,
                                               stdout=subprocess.PIPE,
                                               stderr=tmp_log_handle)
        procs['pv'] = create_pv_proc(procs['xtrabackup'].stdout)
        log.info('Uploading backup to s3://{buk}/{loc}'
                 ''.format(buk=environment_specific.BACKUP_BUCKET_UPLOAD_MAP[host_utils.get_iam_role()],
                           loc=backup_file))
        safe_uploader.safe_upload(precursor_procs=procs,
                                  bucket=environment_specific.BACKUP_BUCKET_UPLOAD_MAP[host_utils.get_iam_role()],
                                  stdin=procs['pv'].stdout,
                                  key=backup_file,
                                  check_func=check_xtrabackup_log,
                                  check_arg=tmp_log)
        log.info('Xtrabackup was successful')
        return backup_file
    except:
        safe_uploader.kill_precursor_procs(procs)
        raise


def check_xtrabackup_log(tmp_log):
    """ Confirm that a xtrabackup backup did not have problems

    Args:
    tmp_log - The path of the log file
    """
    with open(tmp_log, 'r') as log_file:
        xtra_log = log_file.readlines()
        if INNOBACKUP_OK not in xtra_log[-1]:
            raise Exception('innobackupex failed. '
                            'log_file: {tmp_log}'.format(tmp_log=tmp_log))


def create_xtrabackup_command(instance, timestamp, tmp_log):
    """ Create a xtrabackup command

    Args:
    instance - A hostAddr object
    timestamp - A timestamp
    tmp_log - A path to where xtrabackup should log

    Returns:
    a list that can be easily ingested by subprocess
    """
    cnf = host_utils.MYSQL_CNF_FILE
    cnf_group = 'mysqld{port}'.format(port=instance.port)
    datadir = host_utils.get_cnf_setting('datadir', instance.port)
    (xtra_user,
     xtra_pass) = mysql_lib.get_mysql_user_for_role(USER_ROLE_XTRABACKUP)
    return XTRABACKUP_CMD.format(datadir=datadir,
                                 xtra_user=xtra_user,
                                 xtra_pass=xtra_pass,
                                 cnf=cnf,
                                 cnf_group=cnf_group,
                                 port=instance.port,
                                 tmp_log=tmp_log).split()


def get_s3_backup(instance, date, backup_type):
    """ Find xbstream file for an instance on s3 on a given day

    Args:
    instance - A hostaddr object for the desired instance
    date - Desired date of restore file
    backup_type - xbstream or mysqldump

    Returns:
    A list of s3 keys
    """
    backup_keys = list()
    prefixes = set()
    try:
        replica_set = instance.get_zk_replica_set()[0]
    except:
        log.debug('Instance {} is not in zk'.format(instance))
        replica_set = None

    if replica_set:
        prefixes.add(BACKUP_SEARCH_PREFIX.format(
                         retention_policy=environment_specific.get_backup_retention_policy(instance),
                         backup_type=backup_type,
                         replica_set=replica_set,
                         hostname=instance.hostname,
                         port=instance.port,
                         timestamp=date))

    prefixes.add(BACKUP_SEARCH_INITIAL_PREFIX.format(
                     backup_type=backup_type,
                     hostname=instance.hostname,
                     port=instance.port,
                     timestamp=date))

    conn = boto.connect_s3()
    for bucket in environment_specific.BACKUP_BUCKET_DOWNLOAD_MAP[host_utils.get_iam_role()]:
        bucket_conn = conn.get_bucket(bucket, validate=False)
        for prefix in prefixes:
            log.info('Looking for backup with prefix '
                     's3://{bucket}/{prefix}'.format(bucket=bucket,
                                                     prefix=prefix))
            bucket_items = bucket_conn.list(prefix=prefix)
            for key in bucket_items:
                if (key.size <= MINIMUM_VALID_BACKUP_SIZE_BYTES):
                    continue

                backup_keys.append(key)

    if not backup_keys:
        msg = ''.join([NO_BACKUP, instance.__str__()])
        raise Exception(msg)
    return backup_keys


def get_metadata_from_backup_file(full_path):
    """ Parse the filename of a backup to determine the source of a backup

    Note: there is a strong assumption that the port number matches 330[0-9]

    Args:
    full_path - Path to a backup file.
                Example: xtrabackup/standard/testmodsharddb-1/testmodsharddb-1-79-3306-2016-05-18-22:34:39.xbstream

    Returns:
    host - A hostaddr object
    creation - a datetime object describing creation date
    """
    filename = os.path.basename(full_path)
    pattern = '([a-z0-9-]+)-(330[0-9])-(\d{4})-(\d{2})-(\d{2}).*'
    res = re.match(pattern, filename)
    host = host_utils.HostAddr(':'.join((res.group(1), res.group(2))))
    creation = datetime.date(int(res.group(3)), int(res.group(4)),
                             int(res.group(5)))
    return host, creation


def start_restore_log(instance, params):
    """ Create a record in xb_restore_status at the start of a restore

    Args:
    instance - A hostaddr for where to log to
    params - Parameters to be used in the INSERT

    Returns:
    The row_id of the created restore log entry
    """
    try:
        conn = mysql_lib.connect_mysql(instance)
    except Exception as e:
        log.warning("Unable to connect to master to log "
                    "our progress: {e}.  Attempting to "
                    "continue with restore anyway.".format(e=e))
        return None

    if not mysql_lib.does_table_exist(instance, 'test', 'xb_restore_status'):
        create_status_table(conn)
    sql = ("REPLACE INTO test.xb_restore_status "
           "SET "
           "restore_source = %(restore_source)s, "
           "restore_type = 's3', "
           "restore_file = %(restore_file)s, "
           "restore_destination = %(source_instance)s, "
           "restore_date = %(restore_date)s, "
           "restore_port = %(restore_port)s, "
           "replication = %(replication)s, "
           "zookeeper = %(zookeeper)s, "
           "started_at = NOW()")
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        log.info(cursor._executed)
        row_id = cursor.lastrowid
    except Exception as e:
        log.warning("Unable to log restore_status: {e}".format(e=e))
        row_id = None

    cursor.close()
    conn.commit()
    conn.close()
    return row_id


def update_restore_log(instance, row_id, params):
    """ Update the restore log

    Args:
    instance - A hostaddr object for where to log to
    row_id - The restore log id to update
    params - The parameters to update
    """
    try:
        conn = mysql_lib.connect_mysql(instance)
    except Exception as e:
        log.warning("Unable to connect to master to log "
                    "our progress: {e}.  Attempting to "
                    "continue with restore anyway.".format(e=e))
        return

    updates_fields = []

    if 'finished_at' in params:
        updates_fields.append('finished_at=NOW()')
    if 'restore_status' in params:
        updates_fields.append('restore_status=%(restore_status)s')
    if 'status_message' in params:
        updates_fields.append('status_message=%(status_message)s')
    if 'replication' in params:
        updates_fields.append('replication=%(replication)s')
    if 'zookeeper' in params:
        updates_fields.append('zookeeper=%(zookeeper)s')
    if 'finished_at' in params:
        updates_fields.append('finished_at=NOW()')

    sql = ("UPDATE test.xb_restore_status SET "
           "{} WHERE id=%(row_id)s".format(', '.join(updates_fields)))
    params['row_id'] = row_id
    cursor = conn.cursor()
    cursor.execute(sql, params)
    log.info(cursor._executed)
    cursor.close()
    conn.commit()
    conn.close()


def get_age_last_restore(replica_set):
    """ Determine age of last successful backup restore

    Args:
    replica_set - A MySQL replica set

    Returns - A tuple of age in days and name of a replica set. This is done
              to make it easy to use multiprocessing.
    """
    zk = host_utils.MysqlZookeeper()
    today = datetime.date.today()
    age = None
    master = zk.get_mysql_instance_from_replica_set(replica_set)
    try:
        conn = mysql_lib.connect_mysql(master)
        cursor = conn.cursor()
        sql = ("SELECT restore_file "
               "FROM test.xb_restore_status "
               "WHERE restore_status='OK' "
               "ORDER BY finished_at DESC "
               "LIMIT 10")
        # The most recent restore is not always using the newest restore file
        # so we will just grab the 10 most recent.
        cursor.execute(sql)
        restores = cursor.fetchall()
    except Exception as e:
        log.error(e)
        return

    for restore in restores:
        _, creation = get_metadata_from_backup_file(restore['restore_file'])
        if age is None or (today - creation).days < age:
            age = (today - creation).days

    return (age, replica_set)


def create_status_table(conn):
    """ Create the restoration status table if it isn't already there.

    Args:
    conn - A connection to the master server for this replica set.
    """
    try:
        cursor = conn.cursor()
        cursor.execute(XB_RESTORE_STATUS)
        cursor.close()
    except Exception as e:
        log.error("Unable to create replication status table "
                  "on master: {e}".format(e=e))
        log.error("We will attempt to continue anyway.")


def xbstream_unpack(xbstream, datadir):
    """ Decompress an xbstream filename into a directory.

    Args:
    xbstream - An xbstream file in S3
    datadir - The datadir on wich to unpack the xbstream
    """
    procs = {}
    procs['s3_download'] = create_s3_download_proc(xbstream)
    procs['pv'] = create_pv_proc(procs['s3_download'].stdout,
                                 size=xbstream.size)
    procs['xbstream'] = create_xbstream_proc(procs['pv'].stdout,
                                             datadir)
    while(not host_utils.check_dict_of_procs(procs)):
        time.sleep(.5)


def innobackup_decompress(datadir, threads=INNOBACKUP_DECOMPRESS_THREADS):
    """ Decompress an unpacked backup compressed with xbstream.

    Args:
    datadir - The datadir on wich to decomrpess ibd files
    threads - A int which signifies how the amount of parallelism.
              Default is INNOBACKUP_DECOMPRESS_THREADS
    """
    cmd = ' '.join(('/usr/bin/innobackupex',
                    '--parallel={threads}',
                    '--decompress',
                    datadir)).format(threads=threads)

    err_log = os.path.join(datadir, 'xtrabackup-decompress.err')
    out_log = os.path.join(datadir, 'xtrabackup-decompress.log')

    with open(err_log, 'w+') as err_handle, open(out_log, 'w') as out_handle:
        log.info(cmd)
        decompress = subprocess.Popen(cmd.split(),
                                      stdout=out_handle,
                                      stderr=err_handle)
        if decompress.wait() != 0:
            raise Exception('Fatal error: innobackupex decompress '
                            'did not return 0')

        err_handle.seek(0)
        log_data = err_handle.readlines()
        if INNOBACKUP_OK not in log_data[-1]:
            msg = ('Fatal error: innobackupex decompress did not end with '
                   '"{}"'.format(INNOBACKUP_OK))
            raise Exception(msg)


def apply_log(datadir, memory=None):
    """ Apply redo logs for an unpacked and uncompressed instance

    Args:
    datadir - The datadir on wich to apply logs
    memory - A string of how much memory can be used to apply logs. Default 10G
    """
    if not memory:
        # Determine how much RAM to use for applying logs based on the
        # system's total RAM size; all our boxes have 32G or more, so
        # this will always be better than before, but not absurdly high.
        memory = psutil.phymem_usage()[0] / 1024 / 1024 / 1024 / 3

    cmd = ' '.join(('/usr/bin/innobackupex',
                    '--apply-log',
                    '--use-memory={memory}G',
                    datadir)).format(memory=memory)

    log_file = os.path.join(datadir, 'xtrabackup-apply-logs.log')
    with open(log_file, 'w+') as log_handle:
        log.info(cmd)
        apply_logs = subprocess.Popen(cmd.split(),
                                      stderr=log_handle)
        if apply_logs.wait() != 0:
            raise Exception('Fatal error: innobackupex apply-logs did not '
                            'return return 0')

        log_handle.seek(0)
        log_data = log_handle.readlines()
        if INNOBACKUP_OK not in log_data[-1]:
            msg = ('Fatal error: innobackupex apply-log did not end with '
                   '"{}"'.format(INNOBACKUP_OK))
            raise Exception(msg)


def parse_xtrabackup_slave_info(port):
    """ Pull master_log and master_log_pos from a xtrabackup_slave_info file
    NOTE: This file has its data as a CHANGE MASTER command. Example:
    CHANGE MASTER TO MASTER_LOG_FILE='mysql-bin.006233', MASTER_LOG_POS=863

    Args:
    port - the port of the instance on localhost

    Returns:
    binlog_file - Binlog file to start reading from
    binlog_pos - Position in binlog_file to start reading
    """
    datadir = host_utils.get_cnf_setting('datadir', port)
    file_path = os.path.join(datadir, 'xtrabackup_slave_info')
    with open(file_path) as f:
        data = f.read()

    file_pattern = ".*MASTER_LOG_FILE='([a-z0-9-.]+)'.*"
    pos_pattern = ".*MASTER_LOG_POS=([0-9]+).*"
    res = re.match(file_pattern, data)
    binlog_file = res.group(1)
    res = re.match(pos_pattern, data)
    binlog_pos = int(res.group(1))

    log.info('Master info: binlog_file: {binlog_file},'
             ' binlog_pos: {binlog_pos}'.format(binlog_file=binlog_file,
                                                binlog_pos=binlog_pos))
    return (binlog_file, binlog_pos)


def parse_xtrabackup_binlog_info(port):
    """ Pull master_log and master_log_pos from a xtrabackup_slave_info file
    Note: This file stores its data as two strings in a file
          deliminted by a tab. Example: "mysql-bin.006231\t1619"

    Args:
    port - the port of the instance on localhost

    Returns:
    binlog_file - Binlog file to start reading from
    binlog_pos - Position in binlog_file to start reading
    """
    datadir = host_utils.get_cnf_setting('datadir', port)
    file_path = os.path.join(datadir, 'xtrabackup_binlog_info')
    with open(file_path) as f:
        data = f.read()

    fields = data.strip().split("\t")
    if len(fields) != 2:
        raise Exception(('Error: Invalid format in '
                         'file {file_path}').format(file_path=file_path))
    binlog_file = fields[0].strip()
    binlog_pos = int(fields[1].strip())

    log.info('Master info: binlog_file: {binlog_file},'
             ' binlog_pos: {binlog_pos}'.format(binlog_file=binlog_file,
                                                binlog_pos=binlog_pos))
    return (binlog_file, binlog_pos)


def pre_exec():
    """ Used to cause s3 downloads to die when the parent dies"""
    prctl.prctl(prctl.PDEATHSIG, signal.SIGTERM)


def create_s3_download_proc(key):
    devnull = open(os.devnull, 'w')
    cmd = [S3_SCRIPT, 'get',
           '-b', key.bucket.name,
           '-k', urllib.quote_plus(key.name)]
    log.info(' '.join(cmd + ['|']))
    return subprocess.Popen(cmd,
                            stdout=subprocess.PIPE,
                            stderr=devnull,
                            preexec_fn=pre_exec)


def create_pv_proc(stdin, size=None):
    cmd = copy.copy(PV)
    if size:
        cmd.append('--size')
        cmd.append(str(size))

    log.info(' '.join(cmd + ['|']))
    return subprocess.Popen(cmd,
                            stdin=stdin,
                            stdout=subprocess.PIPE)


def create_xbstream_proc(stdin, datadir):
    cmd = copy.copy(XBSTREAM)
    cmd.append('--directory={}'.format(datadir))
    log.info(' '.join(cmd))
    return subprocess.Popen(cmd,
                            stdin=stdin,
                            stdout=subprocess.PIPE)
