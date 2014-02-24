from configparser import RawConfigParser
from binascii import hexlify
from itertools import islice
import errno
import os
import shutil
import struct
import sys
from zlib import crc32

from .hashindex import NSIndex
from .helpers import Error, IntegrityError, read_msgpack, write_msgpack, unhexlify, UpgradableLock
from .lrucache import LRUCache

MAX_OBJECT_SIZE = 20 * 1024 * 1024
MAGIC = b'ATTICSEG'
TAG_PUT = 0
TAG_DELETE = 1
TAG_COMMIT = 2


class Repository(object):
    """Filesystem based transactional key value store

    On disk layout:
    dir/README
    dir/config
    dir/data/<X / SEGMENTS_PER_DIR>/<X>
    dir/index.X
    dir/hints.X
    """
    DEFAULT_MAX_SEGMENT_SIZE = 5 * 1024 * 1024
    DEFAULT_SEGMENTS_PER_DIR = 10000

    class DoesNotExist(Error):
        """Repository {} does not exist"""

    class AlreadyExists(Error):
        """Repository {} already exists"""

    class InvalidRepository(Error):
        """{} is not a valid repository"""

    class CheckNeeded(Error):
        '''Inconsistency detected. Please run "attic check {}"'''

    def __init__(self, path, create=False):
        self.path = path
        self.io = None
        self.lock = None
        self.index = None
        self._active_txn = False
        if create:
            self.create(path)
        self.open(path)

    def __del__(self):
        self.close()

    def create(self, path):
        """Create a new empty repository at `path`
        """
        if os.path.exists(path) and (not os.path.isdir(path) or os.listdir(path)):
            raise self.AlreadyExists(path)
        if not os.path.exists(path):
            os.mkdir(path)
        with open(os.path.join(path, 'README'), 'w') as fd:
            fd.write('This is an Attic repository\n')
        os.mkdir(os.path.join(path, 'data'))
        config = RawConfigParser()
        config.add_section('repository')
        config.set('repository', 'version', '1')
        config.set('repository', 'segments_per_dir', self.DEFAULT_SEGMENTS_PER_DIR)
        config.set('repository', 'max_segment_size', self.DEFAULT_MAX_SEGMENT_SIZE)
        config.set('repository', 'id', hexlify(os.urandom(32)).decode('ascii'))
        with open(os.path.join(path, 'config'), 'w') as fd:
            config.write(fd)

    def get_index_transaction_id(self):
        indicies = sorted((int(name[6:]) for name in os.listdir(self.path) if name.startswith('index.') and name[6:].isdigit()))
        if indicies:
            return indicies[-1]
        else:
            return None

    def get_transaction_id(self):
        index_transaction_id = self.get_index_transaction_id()
        segments_transaction_id = self.io.get_segments_transaction_id()
        if index_transaction_id is not None and segments_transaction_id is None:
            raise self.CheckNeeded(self.path)
        # Attempt to automatically rebuild index if we crashed between commit
        # tag write and index save
        if index_transaction_id != segments_transaction_id:
            if index_transaction_id is not None and index_transaction_id > segments_transaction_id:
                replay_from = None
            else:
                replay_from = index_transaction_id
            self.replay_segments(replay_from, segments_transaction_id)
        return self.get_index_transaction_id()

    def open(self, path):
        self.path = path
        if not os.path.isdir(path):
            raise self.DoesNotExist(path)
        self.config = RawConfigParser()
        self.config.read(os.path.join(self.path, 'config'))
        if not 'repository' in self.config.sections() or self.config.getint('repository', 'version') != 1:
            raise self.InvalidRepository(path)
        self.lock = UpgradableLock(os.path.join(path, 'config'))
        self.max_segment_size = self.config.getint('repository', 'max_segment_size')
        self.segments_per_dir = self.config.getint('repository', 'segments_per_dir')
        self.id = unhexlify(self.config.get('repository', 'id').strip())
        self.io = LoggedIO(self.path, self.max_segment_size, self.segments_per_dir)

    def close(self):
        if self.lock:
            if self.io:
                self.io.close()
            self.io = None
            self.lock.release()
            self.lock = None

    def commit(self):
        """Commit transaction
        """
        self.io.write_commit()
        self.compact_segments()
        self.write_index()
        self.rollback()

    def get_read_only_index(self, transaction_id):
        if transaction_id is None:
            return {}
        return NSIndex((os.path.join(self.path, 'index.%d') % transaction_id).encode('utf-8'), readonly=True)

    def get_index(self, transaction_id, do_cleanup=True):
        self._active_txn = True
        self.lock.upgrade()
        if transaction_id is None:
            self.index = NSIndex.create(os.path.join(self.path, 'index.tmp').encode('utf-8'))
            self.segments = {}
            self.compact = set()
        else:
            if do_cleanup:
                self.io.cleanup(transaction_id)
            shutil.copy(os.path.join(self.path, 'index.%d' % transaction_id),
                        os.path.join(self.path, 'index.tmp'))
            self.index = NSIndex(os.path.join(self.path, 'index.tmp').encode('utf-8'))
            hints = read_msgpack(os.path.join(self.path, 'hints.%d' % transaction_id))
            if hints[b'version'] != 1:
                raise ValueError('Unknown hints file version: %d' % hints['version'])
            self.segments = hints[b'segments']
            self.compact = set(hints[b'compact'])

    def write_index(self):
        hints = {b'version': 1,
                 b'segments': self.segments,
                 b'compact': list(self.compact)}
        transaction_id = self.io.get_segments_transaction_id()
        write_msgpack(os.path.join(self.path, 'hints.%d' % transaction_id), hints)
        self.index.flush()
        os.rename(os.path.join(self.path, 'index.tmp'),
                  os.path.join(self.path, 'index.%d' % transaction_id))
        # Remove old indices
        current = '.%d' % transaction_id
        for name in os.listdir(self.path):
            if not name.startswith('index.') and not name.startswith('hints.'):
                continue
            if name.endswith(current):
                continue
            os.unlink(os.path.join(self.path, name))
        self.index = None

    def compact_segments(self):
        """Compact sparse segments by copying data into new segments
        """
        if not self.compact:
            return
        index_transaction_id = self.get_index_transaction_id()
        segments = self.segments
        for segment in sorted(self.compact):
            if self.io.segment_exists(segment):
                for tag, key, data in self.io.iter_objects(segment, include_data=True):
                    if tag == TAG_PUT and self.index.get(key, (-1, -1))[0] == segment:
                        new_segment, offset = self.io.write_put(key, data)
                        self.index[key] = new_segment, offset
                        segments.setdefault(new_segment, 0)
                        segments[new_segment] += 1
                        segments[segment] -= 1
                    elif tag == TAG_DELETE:
                        if index_transaction_id is None or segment > index_transaction_id:
                            self.io.write_delete(key)
                assert segments[segment] == 0

        self.io.write_commit()
        for segment in sorted(self.compact):
            assert self.segments.pop(segment) == 0
            self.io.delete_segment(segment)
        self.compact = set()

    def replay_segments(self, index_transaction_id, segments_transaction_id):
        self.get_index(index_transaction_id, do_cleanup=False)
        for segment, filename in self.io.segment_iterator():
            if index_transaction_id is not None and segment <= index_transaction_id:
                continue
            if segment > segments_transaction_id:
                break
            self.segments[segment] = 0
            for tag, key, offset in self.io.iter_objects(segment):
                if tag == TAG_PUT:
                    try:
                        s, _ = self.index[key]
                        self.compact.add(s)
                        self.segments[s] -= 1
                    except KeyError:
                        pass
                    self.index[key] = segment, offset
                    self.segments[segment] += 1
                elif tag == TAG_DELETE:
                    try:
                        s, _ = self.index.pop(key)
                        self.segments[s] -= 1
                        self.compact.add(s)
                    except KeyError:
                        pass
                    self.compact.add(segment)
                elif tag == TAG_COMMIT:
                    continue
                else:
                    raise self.CheckNeeded(self.path)
            if self.segments[segment] == 0:
                self.compact.add(segment)
        self.write_index()
        self.rollback()

    def check(self, progress=False, repair=False):
        """Check repository consistency

        This method verifies all segment checksums and makes sure
        the index is consistent with the data stored in the segments.
        """
        error_found = False
        def report_progress(msg, error=False):
            nonlocal error_found
            if error:
                error_found = True
            if error or progress:
                print(msg, file=sys.stderr)
                sys.stderr.flush()

        assert not self._active_txn
        report_progress('Starting repository check...')
        try:
            transaction_id = self.get_transaction_id()
            current_index = self.get_read_only_index(transaction_id)
        except Exception:
            transaction_id = self.io.get_segments_transaction_id()
            current_index = None
        if transaction_id is None:
            transaction_id = self.get_index_transaction_id()
        if transaction_id is None:
            transaction_id = self.io.get_latest_segment()
        segments_transaction_id = self.io.get_segments_transaction_id()
        self.get_index(None)
        for segment, filename in self.io.segment_iterator():
            if segment > transaction_id:
                continue
            try:
                objects = list(self.io.iter_objects(segment))
            except (IntegrityError, struct.error):
                report_progress('Error reading segment {}'.format(segment), error=True)
                objects = []
                if repair:
                    self.io.recover_segment(segment, filename)
                    objects = list(self.io.iter_objects(segment))
            self.segments[segment] = 0
            for tag, key, offset in objects:
                if tag == TAG_PUT:
                    try:
                        s, _ = self.index[key]
                        self.compact.add(s)
                        self.segments[s] -= 1
                    except KeyError:
                        pass
                    self.index[key] = segment, offset
                    self.segments[segment] += 1
                elif tag == TAG_DELETE:
                    try:
                        s, _ = self.index.pop(key)
                        self.segments[s] -= 1
                        self.compact.add(s)
                        self.compact.add(segment)
                    except KeyError:
                        pass
                elif tag == TAG_COMMIT:
                    continue
                else:
                    report_progress('Unexpected tag {} in segment {}'.format(tag, segment), error=True)
        # We might need to add a commit tag if no committed segment is found
        if repair and segments_transaction_id is None:
            report_progress('Adding commit tag to segment {}'.format(transaction_id))
            self.io.segment = transaction_id + 1
            self.io.write_commit()
            self.io.close_segment()
        if current_index and not repair:
            if len(current_index) != len(self.index):
                report_progress('Index object count mismatch. {} != {}'.format(len(current_index), len(self.index)), error=True)
            elif current_index:
                for key, value in self.index.iteritems():
                    if current_index.get(key, (-1, -1)) != value:
                        report_progress('Index mismatch for key {}. {} != {}'.format(key, value, current_index.get(key, (-1, -1))), error=True)
        if not error_found:
            report_progress('Repository check complete, no problems found.')
        if repair:
            self.compact_segments()
            self.write_index()
        else:
            os.unlink(os.path.join(self.path, 'index.tmp'))
        self.rollback()
        return not error_found or repair

    def rollback(self):
        """
        """
        self.index = None
        self._active_txn = False

    def __len__(self):
        if not self.index:
            self.index = self.get_read_only_index(self.get_transaction_id())
        return len(self.index)

    def list(self, limit=None, marker=None):
        if not self.index:
            self.index = self.get_read_only_index(self.get_transaction_id())
        return [id_ for id_, _ in islice(self.index.iteritems(marker=marker), limit)]

    def get(self, id_):
        if not self.index:
            self.index = self.get_read_only_index(self.get_transaction_id())
        try:
            segment, offset = self.index[id_]
            return self.io.read(segment, offset, id_)
        except KeyError:
            raise self.DoesNotExist(self.path)

    def get_many(self, ids, is_preloaded=False):
        for id_ in ids:
            yield self.get(id_)

    def put(self, id, data, wait=True):
        if not self._active_txn:
            self.get_index(self.get_transaction_id())
        try:
            segment, _ = self.index[id]
            self.segments[segment] -= 1
            self.compact.add(segment)
            segment = self.io.write_delete(id)
            self.segments.setdefault(segment, 0)
            self.compact.add(segment)
        except KeyError:
            pass
        segment, offset = self.io.write_put(id, data)
        self.segments.setdefault(segment, 0)
        self.segments[segment] += 1
        self.index[id] = segment, offset

    def delete(self, id, wait=True):
        if not self._active_txn:
            self.get_index(self.get_transaction_id())
        try:
            segment, offset = self.index.pop(id)
        except KeyError:
            raise self.DoesNotExist(self.path)
        self.segments[segment] -= 1
        self.compact.add(segment)
        segment = self.io.write_delete(id)
        self.compact.add(segment)
        self.segments.setdefault(segment, 0)

    def preload(self, ids):
        """Preload objects (only applies to remote repositories
        """


class LoggedIO(object):

    header_fmt = struct.Struct('<IIB')
    assert header_fmt.size == 9
    put_header_fmt = struct.Struct('<IIB32s')
    assert put_header_fmt.size == 41
    header_no_crc_fmt = struct.Struct('<IB')
    assert header_no_crc_fmt.size == 5
    crc_fmt = struct.Struct('<I')
    assert crc_fmt.size == 4

    _commit = header_no_crc_fmt.pack(9, TAG_COMMIT)
    COMMIT = crc_fmt.pack(crc32(_commit)) + _commit

    def __init__(self, path, limit, segments_per_dir, capacity=90):
        self.path = path
        self.fds = LRUCache(capacity)
        self.segment = 0
        self.limit = limit
        self.segments_per_dir = segments_per_dir
        self.offset = 0
        self._write_fd = None

    def close(self):
        for segment in list(self.fds.keys()):
            self.fds.pop(segment).close()
        self.close_segment()
        self.fds = None  # Just to make sure we're disabled

    def segment_iterator(self, reverse=False):
        for dirpath, dirs, filenames in os.walk(os.path.join(self.path, 'data')):
            dirs.sort(key=int, reverse=reverse)
            filenames = sorted((filename for filename in filenames if filename.isdigit()), key=int, reverse=reverse)
            for filename in filenames:
                yield int(filename), os.path.join(dirpath, filename)


    def get_latest_segment(self):
        for segment, filename in self.segment_iterator(reverse=True):
            return segment
        return None

    def get_segments_transaction_id(self):
        """Verify that the transaction id is consistent with the index transaction id
        """
        for segment, filename in self.segment_iterator(reverse=True):
            if self.is_committed_segment(filename):
                return segment
        return None

    def cleanup(self, transaction_id):
        """Delete segment files left by aborted transactions
        """
        self.segment = transaction_id + 1
        for segment, filename in self.segment_iterator(reverse=True):
            if segment > transaction_id:
                os.unlink(filename)
            else:
                break

    def is_committed_segment(self, filename):
        """Check if segment ends with a COMMIT_TAG tag
        """
        with open(filename, 'rb') as fd:
            try:
                fd.seek(-self.header_fmt.size, os.SEEK_END)
            except Exception as e:
                # return False if segment file is empty or too small
                if e.errno == errno.EINVAL:
                    return False
                raise e
            return fd.read(self.header_fmt.size) == self.COMMIT

    def segment_filename(self, segment):
        return os.path.join(self.path, 'data', str(segment // self.segments_per_dir), str(segment))

    def get_write_fd(self, no_new=False):
        if not no_new and self.offset and self.offset > self.limit:
            self.close_segment()
        if not self._write_fd:
            if self.segment % self.segments_per_dir == 0:
                dirname = os.path.join(self.path, 'data', str(self.segment // self.segments_per_dir))
                if not os.path.exists(dirname):
                    os.mkdir(dirname)
            self._write_fd = open(self.segment_filename(self.segment), 'ab')
            self._write_fd.write(MAGIC)
            self.offset = 8
        return self._write_fd

    def get_fd(self, segment):
        try:
            return self.fds[segment]
        except KeyError:
            fd = open(self.segment_filename(segment), 'rb')
            self.fds[segment] = fd
            return fd

    def delete_segment(self, segment):
        try:
            os.unlink(self.segment_filename(segment))
        except OSError:
            pass

    def segment_exists(self, segment):
        return os.path.exists(self.segment_filename(segment))

    def iter_objects(self, segment, include_data=False):
        fd = self.get_fd(segment)
        fd.seek(0)
        if fd.read(8) != MAGIC:
            raise IntegrityError('Invalid segment header')
        offset = 8
        header = fd.read(self.header_fmt.size)
        while header:
            crc, size, tag = self.header_fmt.unpack(header)
            if size > MAX_OBJECT_SIZE:
                raise IntegrityError('Invalid segment object size')
            rest = fd.read(size - self.header_fmt.size)
            if crc32(rest, crc32(memoryview(header)[4:])) & 0xffffffff != crc:
                raise IntegrityError('Segment checksum mismatch')
            if tag not in (TAG_PUT, TAG_DELETE, TAG_COMMIT):
                raise IntegrityError('Invalid segment entry header')
            key = None
            if tag in (TAG_PUT, TAG_DELETE):
                key = rest[:32]
            if include_data:
                yield tag, key, rest[32:]
            else:
                yield tag, key, offset
            offset += size
            header = fd.read(self.header_fmt.size)

    def recover_segment(self, segment, filename):
        self.fds.pop(segment).close()
        # FIXME: save a copy of the original file
        with open(filename, 'rb') as fd:
            data = memoryview(fd.read())
        os.rename(filename, filename + '.beforerecover')
        print('attempting to recover ' + filename, file=sys.stderr)
        with open(filename, 'wb') as fd:
            fd.write(MAGIC)
            while len(data) >= self.header_fmt.size:
                crc, size, tag = self.header_fmt.unpack(data[:self.header_fmt.size])
                if size < self.header_fmt.size or size > len(data):
                    data = data[1:]
                    continue
                if crc32(data[4:size]) & 0xffffffff != crc:
                    data = data[1:]
                    continue
                fd.write(data[:size])
                data = data[size:]

    def read(self, segment, offset, id):
        if segment == self.segment and self._write_fd:
            self._write_fd.flush()
        fd = self.get_fd(segment)
        fd.seek(offset)
        header = fd.read(self.put_header_fmt.size)
        crc, size, tag, key = self.put_header_fmt.unpack(header)
        if size > MAX_OBJECT_SIZE:
            raise IntegrityError('Invalid segment object size')
        data = fd.read(size - self.put_header_fmt.size)
        if crc32(data, crc32(memoryview(header)[4:])) & 0xffffffff != crc:
            raise IntegrityError('Segment checksum mismatch')
        if tag != TAG_PUT or id != key:
            raise IntegrityError('Invalid segment entry header')
        return data

    def write_put(self, id, data):
        size = len(data) + self.put_header_fmt.size
        fd = self.get_write_fd()
        offset = self.offset
        header = self.header_no_crc_fmt.pack(size, TAG_PUT)
        crc = self.crc_fmt.pack(crc32(data, crc32(id, crc32(header))) & 0xffffffff)
        fd.write(b''.join((crc, header, id, data)))
        self.offset += size
        return self.segment, offset

    def write_delete(self, id):
        fd = self.get_write_fd()
        header = self.header_no_crc_fmt.pack(self.put_header_fmt.size, TAG_DELETE)
        crc = self.crc_fmt.pack(crc32(id, crc32(header)) & 0xffffffff)
        fd.write(b''.join((crc, header, id)))
        self.offset += self.put_header_fmt.size
        return self.segment

    def write_commit(self):
        fd = self.get_write_fd(no_new=True)
        header = self.header_no_crc_fmt.pack(self.header_fmt.size, TAG_COMMIT)
        crc = self.crc_fmt.pack(crc32(header) & 0xffffffff)
        fd.write(b''.join((crc, header)))
        self.close_segment()

    def close_segment(self):
        if self._write_fd:
            self.segment += 1
            self.offset = 0
            os.fsync(self._write_fd)
            self._write_fd.close()
            self._write_fd = None
