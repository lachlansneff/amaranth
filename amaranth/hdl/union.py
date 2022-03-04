from collections import OrderedDict
from enum import Enum
from .. import tracer
from .ast import *

class Variant:
    def __init__(self, tag, member, src_loc_at=0):
        if not isinstance(member, Enum):
            raise TypeError("member must be an instance of an Enum, not {!r}".format(member))
        
        self.tag = tag
        self.name = member.name
        self.shape = member.value

        try:
            Shape.cast(self.shape, src_loc_at=1 + src_loc_at)
        except Exception:
            raise TypeError("Variant {!r} has invalid shape: should be castable to Shape".format(self.shape))

        self.width = 

        pass

class Layout:
    def __init__(self, enum) -> None:
        if not issubclass(enum, Enum):
            raise TypeError("enum must be a subclass of Enum, not {!r}".format(enum))
        
        self.variants = OrderedDict()
        tag = 0
        for member in enum:
            self.variants[tag] = Variant(tag, member)
            tag += 1
        
        

class Union:
    def __init__(self, enum, *, name=None, src_loc_at=0):
        if name is None:
            name = tracer.get_var_name(depth=2 + src_loc_at, default=None)
        
        self.name = name
        self.src_loc = tracer.get_src_loc(src_loc_at)

        