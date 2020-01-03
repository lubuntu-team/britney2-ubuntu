from britney2.policies import PolicyVerdict


class DependencySpec(object):

    def __init__(self, deptype, architecture=None):
        self.deptype = deptype
        self.architecture = architecture
        assert self.architecture != 'all', "all not allowed for DependencySpec"


class DependencyState(object):

    def __init__(self, dep):
        """State of a dependency

        :param dep: the excuse that we are depending on

        """
        self.valid = True
        self.verdict = PolicyVerdict.PASS
        self.dep = dep

    @property
    def possible(self):
        return True

    def invalidate(self, verdict):
        self.valid = False
        if verdict > self.verdict:
            self.verdict = verdict


class ImpossibleDependencyState(DependencyState):
    """Object tracking an impossible dependency"""

    def __init__(self, verdict, desc):
        """

        :param desc: description of the impossible dependency

        """
        self.valid = False
        self.verdict = verdict
        self.desc = desc
        self.dep = None

    @property
    def possible(self):
        return False
