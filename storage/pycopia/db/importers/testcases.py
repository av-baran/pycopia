#!/usr/bin/python2.6 -E

# vim:ts=4:sw=4:softtabstop=4:smarttab:expandtab

# this module is a real hack job, but at least it works.
# It imports Test, TestSuite, and UseCase objects from code into the database.

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division



# python imports
import sys
import os
import re
import textwrap
from pytz import timezone
from io import StringIO

from datetime import datetime

# Pycopia imports
from pycopia import logging
from pycopia import aid
from pycopia.WWW import XHTML, rst
from pycopia.XML import XMLPathError
from pycopia.XML.POM import ElementNode, escape
from pycopia import getopt
from pycopia import passwd

from pycopia.db import models
from pycopia.db import types

from pycopia.QA import core
from pycopia import module
from pycopia.QA import testloader
from pycopia.QA import config


_DEBUG = False
_FORCE = False
UTC = timezone('UTC')

_dbsession = None

DEFAULT_AUTHOR     = "tester"


def set_debug(state):
    """Change state of global debug flag."""
    global _DEBUG
    _DEBUG = bool(state)

def set_force(state):
    """Change state of global "forceful" flag."""
    global _FORCE
    _FORCE = bool(state)


class TestCaseData(object):
    """Collect TestCase record data here.

    Call create() at the end when all data collected.
    """

    _HEADING_MAP = {
         'purpose'            : 'purpose',
         'pass criteria'      : 'passcriteria',
         'pass-criteria'      : 'passcriteria',
         'start condition'    : 'startcondition',
         'start-condition'    : 'startcondition',
         'end condition'      : 'endcondition',
         'end-condition'      : 'endcondition',
         'reference'          : 'reference',
         'requirement'        : 'reference',
         'prerequisite'       : 'prerequisites',
         'prerequisites'      : 'prerequisites',
         'prerequiste'        : 'prerequisites',
         'prerequistes'       : 'prerequisites', # Deal with a template typo bug. :-o
         'procedure'          : 'procedure',
    }

    def __init__(self):
        data = self._data = {}
        # Pre-populate all column names with default data.
        data["name"]               = None             # mandatory
        data["lastchange"]         = datetime.now(UTC)
        data["lastchangeauthor"]   = None
        data["author"]             = None             # mandatory
        data["reviewer"]           = None             # mandatory
        data["tester"]             = None             # mandatory
        data["reference"]          = None
        data["purpose"]            = "TODO"     # mandatory
        data["passcriteria"]       = "TODO"      # mandatory
        data["startcondition"]     = None       # mandatory
        data["endcondition"]       = None
        data["procedure"]          = "See code."      # mandatory
        data["comments"]           = None
        data["priority"]           = types.PriorityType.get_default()
        data["cycle"]              = types.TestCaseType.get_default()
        data["status"]             = types.TestCaseStatus.get_default()
        data["automated"]          = True             # mandatory
        data["interactive"]        = False            # mandatory
        data["valid"]              = True             # mandatory
        data["testimplementation"] = None
        data["bugid"]              = None
        # many2many fields
        data = self._many2many = {}
        data["dependents"]     = []
        data["functionalarea"] = []
        data["prerequisites"]  = []

    def set_from_TestCase(self, testcase):
        """Extract available data from Test instance."""
        data = self._data
        data["name"] = mangle_test_name(testcase.test_name)
        if 'unittest' in testcase.test_name:
            data["cycle"] = types.TestCaseType.enumerations[0]
        cl = testcase.__class__
        data["testimplementation"] = "%s.%s" % (cl.__module__, cl.__name__)
        # Set authors according to what module it's in.
        mod = sys.modules[cl.__module__]
        author_name = get_author_from_module(mod)
        author = get_or_create_User(author_name)
        if author is None:
            author = get_or_create_User(DEFAULT_AUTHOR)
        data["author"]     = author
        data["reviewer"] = author
        data["tester"]     = author
        data["lastchangeauthor"] = author
        docstring = cl.__doc__
        if docstring:
            self.parse_docstring(docstring)
        self.resolve_prerequisite(testcase)
        self.resolve_reference()

    def __setitem__(self, name, value):
        if name not in self._data and name not in self._many2many:
            raise ValueError("Invalid column name: %r" % (name,))
        if name == "prerequisites":
            self._many2many["prerequisites"] = value
        elif name == "functionalareas":
            self._many2many[name] = value.split()
        elif name == "functionalarea":
            self._many2many["functionalarea"].append(value.strip())
        else:
            self._data[name] = value

    def __getitem__(self, key):
        try:
            return self._data[key]
        except KeyError:
            return self._many2many[key]

    def parse_docstring(self, text):
        renderer = rst.get_renderer()
        doc = XHTML.new_document()
        parser = doc.get_parser()
        text = textwrap.dedent(text)

        # If not in section format, just add it to Purpose section.
        if text.find("+++") < 0:
            self.__setitem__("purpose", " ".join(text.split())) # normalized text.
            return

        # Convert RST docstring to XHTML/POM object model.
        xhtml = renderer(text)
        parser.feed(xhtml)
        parser.close()
        del parser
        if _DEBUG:
            print ("=== parse_docstring: Original text ===:")
            print (text)
            print ("--- parse_docstring: document text ---:")
            print (doc)

        if not doc.root.id:
            for div in doc.root.find_elements("div"):
                self._do_div(div)            # more than one section
        else:
                self._do_div(doc.root) # one section

    def _do_div(self, div):
        try: # It seems the renderer sometimes does different things...
            name = div.get_element("h1").get_element("a").name
        except (AttributeError, XMLPathError):
            name = div.id
        name = name.lower()
        name = TestCaseData._HEADING_MAP.get(name, name)
        if name.startswith("prerequisite"):
            for p in div.find_elements("p"):
                prereq = " ".join(p.get_text().split())
                self.__setitem__(name, prereq.split())
            return
        body = StringIO()
        for node in div:
            if isinstance(node, ElementNode) and node.__class__._name.startswith("h"):
                continue
            #node.emit(body)
            body.write(escape(node.get_text()))
            body.write(" ")
        body.write("\n")
        self.__setitem__(name, body.getvalue().strip())

    def resolve_prerequisite(self, testinstance):
        doc_prereqs = self._many2many["prerequisites"]
        self._many2many["prerequisites"] = []
        memo = set()
        for prereq in aid.removedups([pr.implementation for pr in testinstance.prerequisites] + doc_prereqs):
            if not _valid_prereq(prereq):
                continue
            if "." not in prereq:
                prereq = "%s.%s" % (testinstance.__class__.__module__, prereq)
            if prereq in memo:
                continue
            memo.add(prereq)
            entry = get_TestEntry_instance(prereq, testinstance.config)
            if entry:
                dbprereq = do_TestEntry(entry)
                self._many2many["prerequisites"].append(dbprereq)
            else:
                logging.warn("prerequisite %r could not be found." % (prereq,))
#            elif issubclass(prereq, core.Test):
#                preinst = prereq(config)
#                entry = core.TestEntry(preinst, (), {}, False)
#                dbprereq = do_TestEntry(entry)
#                self._many2many["prerequisites"].append(dbprereq)

    def resolve_reference(self):
        if self._data["reference"]:
            refname = self._data["reference"]
            try:
                ref = _dbsession.query(models.Requirement).filter(models.Requirement.uri == refname).one()
            except models.NoResultFound:
                ref = models.create(models.Requirement, uri=refname)
            self._data["reference"] = ref
        else:
            self._data["reference"] = None

    def create(self):
        """Create and save new TestCase with data collected so far.
        """
        dbcase = models.create(models.TestCase, **self._data)
        _dbsession.add(dbcase)
        for key, value in self._many2many.items():
            setattr(dbcase, key, value)
        _dbsession.commit()
        return dbcase

    def update(self, dbtestcase):
        """Update a given TestCase instance to the values contained in this holder.
        """
        for key, value in self._data.items():
            setattr(dbtestcase, key, value)
        for key, value in self._many2many.items():
            setattr(dbtestcase, key, value)
        _dbsession.commit()


class UseCaseData(object):
    """Collect UseCase record data here.
    """
    def __init__(self):
        data = self._data = {}
        # Pre-populate all column names with default data.
        data["name"]               = None             # mandatory
        data["purpose"]         = None
        data["notes"]         = None

    def create(self):
        dbcase = models.create(models.UseCase, **self._data)
        _dbsession.add(dbcase)
        _dbsession.commit()
        return dbcase

    def update(self, dbusecase):
        for key, value in self._data.items():
            if value is not None:
                setattr(dbusecase, key, value)
        _dbsession.commit()

    def set_from_UseCase(self, ucclass):
        """Extract available data from UseCase instance."""
        data = self._data
        data["name"] = mangle_test_name("{}.{}".format(ucclass.__module__, ucclass.__name__))
        data["purpose"] = textwrap.dedent(ucclass.__doc__ if ucclass.__doc__ is not None else "")


_MODNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9\.]+$")

def _valid_prereq(pathname):
    pathname = pathname.strip()
    if not pathname:
        return False
    if pathname.lower().startswith("none"):
        return False
    if _MODNAME_RE.match(pathname):
        return True
    return False


def get_TestEntry_instance(string, config):
    """Return a TestEntry instance from a string representing a test class
    plus arguments.
    """
    paren_i = string.find("(")
    if paren_i > 0:
        args = string[paren_i+1:-1]
        string = string[:paren_i]
        args, kwargs = core.parse_args(args)
    else:
        args = ()
        kwargs = {}
    try:
        cls = module.get_object(string)
    except (module.ModuleImportError, module.ObjectImportError), err:
        logging.warn(err)
        return None
    testinstance = cls(config)
    return core.TestEntry(testinstance, args, kwargs, False)


def do_module(mod, config):
    """Import objects in the given module."""
    try:
        suite = testloader.get_TestSuite_from_module(mod, config)
    except module.ObjectImportError, err:
        pass
    else:
        do_TestSuite(suite)

    # No suite factory function, so just import objects from module.
    for name in dir(mod):
        obj = getattr(mod, name)
        if not callable(obj) or type(obj) is not type:
            continue
        if issubclass(obj, core.TestSuite):
            testsuite = obj(config)
            do_TestSuite(testsuite)
            continue
        if issubclass(obj, core.Test):
            if obj.__doc__:
                test = obj(config)
                do_Test(test)
                continue
        if issubclass(obj, core.UseCase):
            do_UseCase(obj)
            continue


def do_TestSuite(suite):
    """Import from a core.TestSuite instance.

    Arguments:
        suite: an instance of core.TestSuite, or subclass.

    Returns:
        A database record that maps to the original suite object, populated
        with the contents of the suite (also imported).
        """
    cl = suite.__class__
    name = mangle_test_name(suite.test_name)
    dbsuite = get_or_create_TestSuite(name=name, valid=True, 
            suiteimplementation="%s.%s" % (cl.__module__, cl.__name__))
    dbsuite.subsuites = []
    dbsuite.testcases = []

    memo = set()
    for testentry in suite:
        if testentry.inst.__class__ in memo:
            continue
        memo.add(testentry.inst.__class__)
        if isinstance(testentry, core.SuiteEntry):
            newsuite = do_TestSuite(testentry.inst)
            dbsuite.subsuites.append(newsuite)
        else: # a TestEntry or TestSeriesEntry
            dbcase = do_TestEntry(testentry)
            dbsuite.testcases.append(dbcase)
    _dbsession.commit()
    return dbsuite


def do_Test(testinstance):
    """Process a core.Test instance outside of a TestEntry (no args).
    """
    name = mangle_test_name(testinstance.test_name)
    try:
        dbcase = _dbsession.query(models.TestCase).filter(models.TestCase.name==name).one()
    except models.NoResultFound:
        dbcase = create_TestCase(testinstance)
    else:
        if _FORCE:
            _dbsession.delete(dbcase)
            _dbsession.commit()
            dbcase = create_TestCase(testinstance)
        else:
            update_TestCase(testinstance, dbcase)
    return dbcase


def do_TestEntry(entry):
    """Import from a core.TestEntry instance.

    This object represents a test instance, with arguments.
    """
    name = mangle_test_name(entry.inst.test_name)
    try:
        dbcase = _dbsession.query(models.TestCase).filter(models.TestCase.name==name).one()
    except models.NoResultFound:
        dbcase = create_TestCase(entry.inst)
    else:
        if _FORCE:
            _dbsession.delete(dbcase)
            _dbsession.commit()
            dbcase = create_TestCase(entry.inst)
        else:
            update_TestCase(entry.inst, dbcase)
    return dbcase


def do_UseCase(ucclass):
    name = mangle_test_name("{}.{}".format(ucclass.__module__, ucclass.__name__))
    try:
        dbuc = _dbsession.query(models.UseCase).filter(models.UseCase.name==name).one()
    except models.NoResultFound:
        dbuc = create_UseCase(ucclass)
    else:
        update_UseCase(ucclass, dbuc)
    return dbuc

def mangle_test_name(name):
    return name.replace("testcases.", "")


def create_TestCase(testinstance):
    testcase_holder = TestCaseData()
    testcase_holder.set_from_TestCase(testinstance)
    return testcase_holder.create()

def update_TestCase(testinstance, dbtestcase):
    testcase_holder = TestCaseData()
    testcase_holder.set_from_TestCase(testinstance)
    return testcase_holder.update(dbtestcase)


def create_UseCase(ucclass):
    holder = UseCaseData()
    holder.set_from_UseCase(ucclass)
    return holder.create()

def update_UseCase(ucclass, dbusecase):
    holder = UseCaseData()
    holder.set_from_UseCase(ucclass)
    return holder.update(dbusecase)


def get_or_create_TestSuite(**kwargs):
    try:
        testsuite = _dbsession.query(models.TestSuite).filter(models.TestSuite.name==kwargs["name"]).one()
    except models.NoResultFound:
        testsuite = models.create(models.TestSuite, **kwargs)
        _dbsession.add(testsuite)
        _dbsession.commit()
    else:
        if _FORCE:
            _dbsession.delete(testsuite)
            _dbsession.commit()
            testsuite = models.create(models.TestSuite, **kwargs)
            _dbsession.add(testsuite)
            _dbsession.commit()
    return testsuite


def get_or_create_User(username):
    try:
        user = _dbsession.query(models.User).filter(models.User.username==username).one()
    except models.NoResultFound:
        pwent = passwd.getpwnam(username)
        user = models.create_user(_dbsession, pwent)
    return user


def get_author_from_module(module):
    if hasattr(module, "__author__"):
        return module.__author__.split("@")[0]
    else:
        return DEFAULT_AUTHOR
# TODO author from SCM metadata



_DOC = """Test case importer.

    Usage:
        tcimport [-df] <basepackage>...
        tcimport [-df] -M <module>...

    where
        -d turn on debugging.
        -f Force updating the database (normally existing records won't be
             overwritten).
        -M Import a module, instead of a package.

         basepackage:     is the base package to start scanning from.

    Example:

        $ tcimport testcases.WWW.xhtml
             -- this imports all testcases under testcases/WWW/xhtml

        $ tcimport testcases
             -- this imports all test cases under the "testcases" package.
    """

class TestCaseImporter(object):

    def _convert_path(self, lenbasepath, dirpath, filename):
        if filename.startswith("_"):
            return None
        if filename.endswith(".py"):
            return os.path.join(dirpath, filename)[lenbasepath:-3].replace("/", ".")
        else:
            return None

    def __call__(self, argv):
        global _DEBUG, _FORCE, _dbsession, debugger
        domodule = False
        opts, longopts, args = getopt.getopt(argv[1:], "h?dMf")
        for opt, arg in opts:
            if opt in ("-h", "-?"):
                print (_DOC)
                return
            elif opt == "-d":
                from pycopia import debugger
                _DEBUG = True
            elif opt == "-f":
                _FORCE = True
            elif opt == "-M":
                domodule = True

        if not args:
            print (_DOC)
            return

        # Look like a test runner.
        self.config = config.get_config()
        _dbsession = models.get_session()
        self.config.options_override = longopts
        self.config.arguments = []
        self.config.username = os.environ["USER"]
        try:
            if domodule:
                for arg in args:
                    self.import_module(arg)
            else:
                for arg in args:
                    self.import_package(arg)
        finally:
            _dbsession.close()
            _dbsession = None

    def import_package(self, basepackage):
        for basepath in module.get_module(basepackage).__path__:
            lenbasepath = len(basepath)-len(basepackage)
            for dirpath, dirnames, filenames in os.walk(basepath):
                for filename in filenames:
                    modname = self._convert_path(lenbasepath, dirpath, filename)
                    if modname:
                        self.import_module(modname)

    def import_module(self, modname):
        if _DEBUG:
            print("Doing module: %s" % modname)
        try:
            mod = module.get_module(modname)
            do_module(mod, self.config)
        except:
            ex, val, tb = sys.exc_info()
            if _DEBUG:
                debugger.post_mortem(tb, ex, val)
            else:
                logging.warn("Could not import %s: %s" % ( modname, "%s: %s" % (ex, val)))



if __name__ == "__main__":
    importer = TestCaseImporter()
    importer(sys.argv)

