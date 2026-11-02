"""Pure command fencing for uniquely owned Office test units; never the live Office slice."""
import re
import uuid


def fixture_slice():
    # Hyphens encode slice ancestry: office-test-*.slice would live INSIDE office.slice.
    return 'office_test_'+uuid.uuid4().hex+'.slice'


def isolated_command(command,*,slice_name=None):
    name=fixture_slice() if slice_name is None else slice_name
    if not isinstance(name,str) or not re.fullmatch(r'office_test_[0-9a-f]{32}\.slice',name):
        raise ValueError('fixture_slice_must_be_flat_and_private')
    assert name!='office.slice' and '-' not in name
    args=list(command)
    positions=[i for i,arg in enumerate(args) if arg.startswith(('--slice=','--property=Slice='))]
    if len(positions)!=1:raise ValueError('fixture_requires_one_slice')
    i=positions[0];args[i]=('--property=Slice=' if args[i].startswith('--property=') else '--slice=')+name
    return args
