from openfisca_us.model_api import *


class c07180(Variable):
    value_type = float
    entity = TaxUnit
    definition_period = YEAR
    label = "Child/dependent care credit"
    unit = USD
    documentation = "Nonrefundable credit for child and dependent care expenses from Form 2441"

    def formula(tax_unit, period, parameters):
        cdcc = parameters(period).irs.credits.cdcc
        if cdcc.refundable or cdcc.abolition:
            return 0
        else:
            c05800 = tax_unit("c05800", period)
            e07300 = tax_unit("e07300", period)
            c33200 = tax_unit("c33200", period)
            c05800_minus_e07300_capped = max_(c05800 - e07300, 0)
            return min_(c05800_minus_e07300_capped, c33200)


cdcc = variable_alias("cdcc", c07180)


class c33200(Variable):
    value_type = float
    entity = TaxUnit
    label = "Credit for child and dependent care expenses"
    unit = USD
    documentation = "From form 2441, before refundability checks"
    definition_period = YEAR

    def formula(tax_unit, period, parameters):
        cdcc = parameters(period).irs.credits.cdcc
        eligible_deps = min_(tax_unit("f2441", period), cdcc.eligibility.max)
        max_credit = eligible_deps * cdcc.max
        c32800 = max_(0, min_(tax_unit("filer_e32800", period), max_credit))
        mars = tax_unit("mars", period)
        person = tax_unit.members
        is_head = person("is_tax_unit_head", period)
        earnings = person("earned", period)
        is_spouse = person("is_tax_unit_spouse", period)
        head_earnings = tax_unit.sum(is_head * earnings)
        spouse_earnings = tax_unit.sum(is_spouse * earnings)
        lowest_earnings = where(
            mars == mars.possible_values.JOINT,
            min_(head_earnings, spouse_earnings),
            head_earnings,
        )
        c33000 = max_(0, min_(c32800, lowest_earnings))
        c00100 = tax_unit("c00100", period)
        tratio = 0.01 * max_(
            ((c00100 - cdcc.phaseout.start) * cdcc.phaseout.rate), 0
        )
        crate = max_(
            cdcc.phaseout.min,
            cdcc.phaseout.max
            - min_(
                cdcc.phaseout.max - cdcc.phaseout.min,
                tratio,
            ),
        )
        tratio2 = max_(
            ((c00100 - cdcc.phaseout.second_start) * cdcc.phaseout.rate / 1e2),
            0,
        )
        crate_if_over_second_threshold = max_(
            0, cdcc.phaseout.min - min_(cdcc.phaseout.min, tratio2)
        )
        crate = where(
            c00100 > cdcc.phaseout.second_start,
            crate_if_over_second_threshold,
            crate,
        )

        return c33000 * crate


class cdcc_refund(Variable):
    value_type = float
    entity = TaxUnit
    definition_period = YEAR
    label = "Child/dependent care refundable credit"
    unit = USD
    documentation = "Refundable credit for child and dependent care expenses from Form 2441"

    def formula(tax_unit, period, parameters):
        cdcc = parameters(period).irs.credits.cdcc
        if cdcc.refundable and not cdcc.abolition:
            return tax_unit("c33200", period)
        else:
            return 0
