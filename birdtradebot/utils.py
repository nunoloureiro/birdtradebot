import json


def prettify_dict(rule):
    """ Prettifies printout of dictionary as string.

        rule: rule

        Return value: rule string
    """
    return json.dumps(rule, sort_keys=False,
                        indent=4, separators=(',', ': '))
