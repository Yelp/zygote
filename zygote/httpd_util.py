class HTTPHeaders(dict):

    @classmethod
    def parse(cls, header_list):
        items = []
        for h in header_list:
            if not h:
                continue
            try:
                k, v = h.split(':', 1)
            except ValueError:
                continue
            k = k.strip()
            v = v.strip()
            items.append((k.lower(), (k, v)))
        return cls(items)

    def __getitem__(self, header):
        k, v = super(HTTPHeaders, self).__getitem__(header.lower())
        return v

    def get(self, header, default=None):
        try:
            return self[header]
        except KeyError:
            return default

    def __contains__(self, header):
        return super(HTTPHeaders, self).__contains__(header.lower())

    def iteritems(self):
        for item_tuple in super(HTTPHeaders, self).itervalues():
            yield item_tuple

    def items(self):
        return list(self.iteritems())

    def iterkeys(self):
        for k, _ in super(HTTPHeaders, self).itervalues():
            yield k

    def keys(self):
        return list(self.iterkeys())

    def itervalues(self):
        for _, v in super(HTTPHeaders, self).itervalues():
            yield v

    def values(self):
        return list(self.itervalues())

class HTTPBody(object):

    BUFSIZE = 16384

    def __init__(self, data):
        self._position = 0
        self._data = data
        self._size = len(data)

    def read(self, size=None):
        to_read = min(self._size - self._position, size or self.BUFSIZE)
        if to_read:
            data = self._data[self._position:self._position + to_read]
            self._position += to_read
            return data
        else:
            return ''

    def readline(self):
        newline_pos = self._data.find('\n', self._position)
        if newline_pos == -1:
            data = self._data[self._position:]
            self._position = self._size
            return data
        else:
            data = self._data[self._position:newline_pos]
            self._position = newline_pos + 1
            return data

    def readlines(self, hint=None):
        while True:
            newline_pos = self._data.find('\n', self._position)
            if newline_pos:
                yield self._data[self._position:newline_pos]
                self._position = newline_pos + 1
            else:
                yield self._data[self._position:]
                break

    def __iter__(self):
        while self._position < self._size:
            to_read = min(self._size - self._position, self.BUFSIZE)
            yield self._data[self._position:self._position + to_read]
            self._position += to_read
