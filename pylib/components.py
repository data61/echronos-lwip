import os
import shutil
import pystache
import xml.etree.ElementTree
from .utils import BASE_DIR, base_path, base_to_top_paths


# FIXME: Use correct declaration vs definition.
_REQUIRED_COMPONENT_SECTIONS = ['public_headers',
                                'public_type_definitions',
                                'public_structure_definitions',
                                'public_object_like_macros',
                                'public_function_like_macros',
                                'public_extern_definitions',
                                'public_function_definitions',
                                'headers',
                                'object_like_macros',
                                'type_definitions',
                                'structure_definitions',
                                'extern_definitions',
                                'function_definitions',
                                'state',
                                'function_like_macros',
                                'functions',
                                'public_functions']


class _SchemaFormatError(RuntimeError):
    """To be raised when a component configuration schema violates assumptions or conventions."""
    pass


def _merge_schema_entries(a, b, path=''):
    """Recursively merge the entries of two XML component schemas.

    'a' and 'b' (instances of xml.etree.ElementTree.Element) are the two schema entries to merge.
    All entries from 'b' are merged into 'a'.
    If 'a' contains an entry a* with the same name as an entry b* in 'b', they can only be merged if both a* and b*
    have child entries themselves.
    If either a* or b* does not have at least one child entry, this function raises a _SchemaFormatError.

    Within each of 'a' and 'b', the names of their entries must be unique.
    In other words, no two entries in 'a' may have the same name.
    The same applies to 'b'.

    When the function returns, 'a' contains all entries from 'b' and 'b' is unmodified.

    """
    a_children = {child.attrib['name']: child for child in a}
    for b_child in b:
        try:
            name = b_child.attrib['name']
        except KeyError:
            raise _SchemaFormatError('A schema entry under "{}" does not contain a name attribute'.format(path))
        if name in a_children:
            try:
                a_child = a_children[name]
            except KeyError:
                raise _SchemaFormatError('A schema entry under "{}" does not contain a name attribute'.format(path))
            if (len(b_child) == 0) != (len(a_child) == 0):
                raise _SchemaFormatError('Unable to merge two schemas: \
the entry {}.{} is present in both schemas, but it has children in one and no children in the other. \
To merge two schemas, corresponding entries both need need to either have child entries or not.'.format(path, name))
            if len(b_child) and len(a_child):
                _merge_schema_entries(a_child, b_child, '{}.{}'.format(path, name))
            else:
                # replace existing entry in a with the entry from b, allowing to override entries
                a.remove(a_child)
                a.append(b_child)
        else:
            a.append(b_child)


def _merge_schema_sections(sections):
    merged_schema = xml.etree.ElementTree.fromstring('<schema>\n</schema>')

    for section in sections:
        schema = xml.etree.ElementTree.fromstring('<schema>\n{}\n</schema>'.format(section))
        _merge_schema_entries(merged_schema, schema)

    return xml.etree.ElementTree.tostring(merged_schema).decode()


def sort_typedefs(typedef_lines):
    """Given a string containing multiple lines of typedefs, sort the lines so that the typedefs are in the 'correct'
    order.

    The typedef lines must only contain typedefs.
    No comments or other data is allowed.
    Blank lines are allowed, but will be ommited from the output.

    The correct order for typedefs is on such that if typedef 'b' is defined in terms of typedef 'a', typedef 'a' will
    appear first in the sorted output.

    """

    typedefs = []
    for l in typedef_lines.split('\n'):
        if l == '':
            continue
        if not l.endswith(';'):
            raise Exception("Expect a typedef line to end with ';' ({})".format(l))
        parts = l[:-1].split()
        if not parts[0] == 'typedef':
            raise Exception("Expect typedef line to startwith 'typedef'")
        new_type = parts[-1]
        old_type = ' '.join(parts[1:-1])
        typedefs.append((new_type, old_type))

    new_types = [new for (new, _) in typedefs]
    r = []

    # First put in any types that don't cross reference.
    #  we assume they are defined in other headers.
    for (new, old) in typedefs[:]:
        if old not in new_types:
            r.append((new, old))
            typedefs.remove((new, old))

    # Now, for each new type
    i = 0
    while i < len(r):
        check_type = r[i][0]
        i += 1
        for (new, old) in typedefs[:]:
            if old == check_type:
                r.append((new, old))
                typedefs.remove((new, old))

    return '\n'.join(['typedef {} {};'.format(old, new) for (new, old) in r])


def _render_data(in_data, name, config):
    """Render input data (`in_data`) using a given `config`. The result is returned."""
    pystache.defaults.MISSING_TAGS = 'strict'
    pystache.defaults.DELIMITERS = ('[[', ']]')
    pystache.defaults.TAG_ESCAPE = lambda u: u
    return pystache.render(in_data, config, name=name)


def _parse_sectioned_file(fn, config={}):
    """Given a sectioned C-like file, returns a dictionary of { section: content }

    For example an input of:
    /*| foo |*/
    foo data....

    /*| bar |*/
    bar data....

    Would produce:

    { 'foo' : "foo data....", 'bar' : "bar data...." }
    """

    with open(fn) as f:
        sections = {}
        current_lines = None
        for line in f.readlines():
            line = line.rstrip()

            if line.startswith('/*|') and line.endswith('|*/'):
                section = line[3:-3].strip()
                current_lines = []
                sections[section] = current_lines
            elif current_lines is not None:
                current_lines.append(line)

    for key, value in sections.items():
        sections[key] = _render_data('\n'.join(value).rstrip(), "{}: Section {}".format(fn, key), config)

    for s in _REQUIRED_COMPONENT_SECTIONS:
        if s not in sections:
            raise Exception("Couldn't find expected section '{}' in file: '{}'".format(s, fn))

    return sections


class Component:
    """Represents an optional, exchangeable piece of functionality of an RTOS.

    Components reside in the components/ directory of the core or sub-projects.
    This class transparently finds components in any of the available core or sub-projects.
    Instances of this class encapsulate the act of parsing a component file and converting it into configuration data
    used when generating an RtosModule.

    """

    @staticmethod
    def find(topdir, partial_path):
        """Find the component partial_path in the core repository or client repositories further up in the directory
        tree."""
        paths = base_to_top_paths(topdir, 'components', partial_path)
        paths.reverse()
        for path in paths:
            if os.path.exists(path):
                return path
        raise KeyError('Unable to find component "{}" in {}'.format(partial_path, paths))

    def __init__(self, name, resource_name=None, configuration={}):
        """Create a component object.

        Such objects encapsulate the act of parsing a corresponding source file.
        The parsed data is converted into configuration information used when generating an RtosModule by rendering an
        RTOS template file.

        'name' is the component name used in the RTOS template file.
        For example, the properties of the interrupt event component are referred to as 'interrupt_event.xyz' in the
        RTOS template files.

        'resource_name' is the base name of the source file of this component that is parsed to obtain this
        component's properties.
        For example, the base name of the interrupt event component is 'interrupt-event', which expands to the on-disk
        file name of interrupt-event.c.

        'configuration' is a dictionary with configuration information.
        It is passed to the '_parse_sectioned_file()' function used to parse this component's source file.

        """
        self.name = name
        if resource_name is not None:
            self._resource_name = resource_name
        else:
            self._resource_name = name
        self._configuration = configuration

    def parse(self, topdir, parsing_configuration={}):
        """Retrieve the properties of this component by parsing its corresponding source file.

        'topdir' is the absolute, normalized path of the directory of the active core or client repository from which
        x.py was invoked.

        'parsing_configuration' is an optional dictionary that is merged with the component's base configuration and
        passed to the parsing function.

        This function returns a dictionary containing configuration information that can be used to render an RTOS
        template.

        """
        if isinstance(parsing_configuration, dict):
            configuration = self._configuration.copy()
            configuration.update(parsing_configuration)
        else:
            configuration = self._configuration

        component = None
        for name in [f.format(self._resource_name) for f in ['{0}.c', '{0}/{0}.c']]:
            try:
                component = Component.find(topdir, name)
                break
            except KeyError:
                pass
        if component is None:
            raise KeyError('Unable to find component "{}"'.format(self._resource_name))

        return _parse_sectioned_file(component, configuration)


class ArchitectureComponent(Component):
    """This refinement of the Component class represents an architecture-specific component.

    This class encapsulates the act of finding the architecture-specific source file corresponding to this component.
    This is opposed to the base Component class which is unaware of architecture-specific file naming conventions.

    """
    def parse(self, topdir, arch):
        """Retrieve the properties of this component by parsing its architecture-specific source file.

        'topdir' is the absolute, normalized path of the directory of the active core or client repository from which
        x.py was invoked.

        'arch', an instance of Architecture, identifies the architecture of the source file to parse.

        Otherwise, this function behaves as Component.parse().

        """
        assert isinstance(arch, Architecture)

        component = None
        for name in [f.format(arch.name, self._resource_name) for f in ['{0}-{1}/{0}-{1}.c', '{1}-{0}.c']]:
            try:
                component = Component.find(topdir, name)
                break
            except KeyError:
                pass
        if component is None:
            raise KeyError('Unable to find component "{}" for architecture {}'.format(self._resource_name, arch.name))

        return _parse_sectioned_file(component, self._configuration)


class Architecture:
    """Represents the properties of a target architecture for which an RtosModule can be generated."""
    def __init__(self, name, configuration):
        assert isinstance(name, str)
        assert isinstance(configuration, dict)
        self.name = name
        self.configuration = configuration


class RtosSkeleton:
    """Represents an RTOS variant as defined by a set of components / functionalities.

    For example, the specific RTOS variant gatria consists exactly of a context-switch and a scheduler component.

    This class encapsulates the act of deriving an RtosModule for a specific configuration and target architecture.

    """
    def __init__(self, name, components, configuration={}):
        """Create an RTOS skeleton based on its core properties.

        'name', a string, is the unique name of the RTOS skeleton.

        'components', a sequence of Component instances, is the set of components that define this RTOS variant.

        'configuration', a dictionary, contains configuration information specific to this RTOS variant.
        It is used when generating an RtosModule from this skeleton.

        """
        assert isinstance(name, str)
        assert isinstance(components, list)
        assert isinstance(configuration, dict)
        self.name = name
        self.python_file = os.path.join(BASE_DIR, 'components', '{}.py'.format(self.name))

        self._components = components
        self._configuration = configuration

    def get_module_sections(self, topdir, arch):
        """Retrieve the sections necessary to generate an RtosModule from this skeleton.

        """
        module_sections = {}
        for component in self._components:
            for name, contents in component.parse(topdir, arch).items():
                if name not in module_sections:
                    module_sections[name] = []
                module_sections[name].append(contents)
        return module_sections

    def create_configured_module(self, topdir, arch):
        """Retrieve module configuration information and create a corresponding RtosModule instance.

        This is only a convenience function.

        """
        return RtosModule(self.name, arch, self.get_module_sections(topdir, arch), self.python_file)


class RtosModule:
    """Represents an instance of an RtosSkeleton (or RTOS variant) with a specific configuration, in particular for a
    specific target architecture.

    This class encapsulates the act of rendering an RTOS template given an RTOS configuration into a module on disk.

    """
    def __init__(self, name, arch, sections, python_file):
        """Create an RtosModule instance.

        'name', a string, is the name of the RTOS, i.e., the same as the underlying RtosSkeleton.

        'arch', an instance of Architecture, is the architecture this module is targetted for.

        'sections', a dictionary, containing all the merged sections from the RTOS components.
        It is typically obtained via RtosSkeleton.get_module_configuration().

        """
        assert isinstance(name, str)
        assert isinstance(arch, Architecture)
        assert isinstance(sections, dict)
        self._name = name
        self._arch = arch
        self._sections = sections
        self._python_file = python_file

    @property
    def _module_name(self):
        return 'rtos-' + self._name

    @property
    def _module_dir(self):
        module_dir = base_path('packages', self._arch.name, self._module_name)
        os.makedirs(module_dir, exist_ok=True)
        return module_dir

    def generate(self):
        """Generate the RTOS module to disk, so it is available as a compile and link unit to projects."""
        self._render()

    def _render(self):
        python_output = os.path.join(self._module_dir, 'entity.py')
        source_output = os.path.join(self._module_dir, self._module_name + '.c')
        header_output = os.path.join(self._module_dir, self._module_name + '.h')
        config_output = os.path.join(self._module_dir, 'schema.xml')

        source_sections = ['headers', 'object_like_macros',
                           'type_definitions', 'structure_definitions',
                           'extern_definitions', 'function_definitions',
                           'state', 'function_like_macros',
                           'functions', 'public_functions']
        header_sections = ['public_headers', 'public_type_definitions',
                           'public_object_like_macros', 'public_function_like_macros',
                           'public_extern_definitions', 'public_function_definitions']
        sections = self._sections

        with open(source_output, 'w') as f:
            for ss in source_sections:
                data = '\n'.join(sections[ss])
                if ss == 'type_definitions':
                    data = sort_typedefs(data)
                f.write(data)
                f.write('\n')

        with open(header_output, 'w') as f:
            mod_name = self._module_name.upper().replace('-', '_')
            f.write("#ifndef {}_H\n".format(mod_name))
            f.write("#define {}_H\n".format(mod_name))
            for ss in header_sections:
                for data in sections[ss]:
                    f.write(data)
                    f.write('\n')
            f.write("\n#endif /* {}_H */".format(mod_name))

        with open(config_output, 'w') as f:
            f.write('''<?xml version="1.0" encoding="UTF-8" ?>
''')
            schema = _merge_schema_sections(sections.get('schema', []))
            f.write(schema)

        shutil.copyfile(self._python_file, python_output)


def build(args):
    # Generate RTOSes
    for rtos_name, arch_names in args.configurations.items():
        generate_rtos_module(args.topdir, args.skeletons[rtos_name],
                             [args.architectures[arch] for arch in arch_names])


def generate_rtos_module(topdir, skeleton, architectures):
    """Generate RTOS modules for several architectures from a given skeleton."""
    for arch in architectures:
        rtos_module = skeleton.create_configured_module(topdir, arch)
        rtos_module.generate()