"""Pure LP-removal severity classifier shared by paper and funded policy layers."""

LP_MINOR, LP_MATERIAL, LP_CATASTROPHIC = "minor", "material", "catastrophic"


def classify_lp_removal(fraction, *, minor_frac, catastrophic_frac, route_degraded=False,
                        route_gone=False):
    if minor_frac is None or catastrophic_frac is None or route_gone or fraction is None:
        return LP_CATASTROPHIC
    if fraction >= float(catastrophic_frac):
        return LP_CATASTROPHIC
    if fraction >= float(minor_frac):
        return LP_MATERIAL
    return LP_MINOR
