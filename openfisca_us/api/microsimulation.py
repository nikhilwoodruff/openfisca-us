from microdf.generic import MicroDataFrame
import numpy as np
from openfisca_core import periods
from openfisca_core.model_api import *
from openfisca_us_data import CPS
import openfisca_us
import pandas as pd
from openfisca_core.simulation_builder import SimulationBuilder
from pathlib import Path
from microdf import MicroSeries
import tables


class Microsimulation:
    def __init__(self, *reforms, dataset=CPS, year=2020):
        tables.file._open_files.close_all()
        self.reforms = reforms
        self.load_dataset(dataset, year)
        self.bonus_sims = {}

    def apply_reforms(self, reforms: list) -> None:
        """Applies a list of reforms to the tax-benefit system.

        Args:
            reforms (list): A list of reforms. Each reform can also be a list of reforms.
        """
        for reform in reforms:
            if isinstance(reform, tuple) or isinstance(reform, list):
                self.apply_reforms(reform)
            else:
                self.system = reform(self.system)

    def load_dataset(self, dataset, year):
        self.system = openfisca_us.CountryTaxBenefitSystem()
        self.apply_reforms(self.reforms)
        builder = SimulationBuilder()
        builder.create_entities(self.system)

        data = dataset.load(year)

        builder.declare_person_entity("person", np.array(data["person_id"]))

        for group_entity in ("tax_unit", "family", "spm_unit", "household"):
            primary_keys = np.array(data[f"{group_entity}_id"])
            group = builder.declare_entity(group_entity, primary_keys)
            foreign_keys = np.array(data[f"person_{group_entity}_id"])
            builder.join_with_persons(
                group, foreign_keys, np.array(["member"] * len(foreign_keys))
            )

        model = builder.build(self.system)

        for variable in data.keys():
            if variable in self.system.variables:
                model.set_input(
                    variable, year, np.array(data[variable]),
                )

        self.simulation = model

    def map_to(
        self, arr: np.array, entity: str, target_entity: str, how: str = None
    ):
        entity_pop = self.simulation.populations[entity]
        target_pop = self.simulation.populations[target_entity]
        GROUP_ENTITIES = ("tax_unit", "family", "spm_unit", "household")
        if entity == "person" and target_entity in GROUP_ENTITIES:
            if how and how not in (
                "sum",
                "any",
                "min",
                "max",
                "all",
                "value_from_first_person",
            ):
                raise ValueError("Not a valid function.")
            return target_pop.__getattribute__(how or "sum")(arr)
        elif entity in GROUP_ENTITIES and target_entity == "person":
            if not how:
                return entity_pop.project(arr)
            if how == "mean":
                return entity_pop.project(arr / entity_pop.nb_persons())
        elif entity == target_entity:
            return arr
        else:
            return self.map_to(
                self.map_to(arr, entity, "person", how="mean"),
                "person",
                target_entity,
                how="sum",
            )

    def calc(
        self, variable, map_to=None, how=None, period=2020, weighted=True
    ):
        var_metadata = self.simulation.tax_benefit_system.variables[variable]
        entity = var_metadata.entity.key
        arr = self.simulation.calculate(variable, period)
        if var_metadata.value_type == float:
            arr = arr.round(2)
        if var_metadata.value_type == Enum:
            arr = arr.decode_to_str()
        if map_to:
            arr = self.map_to(arr, entity, map_to, how=how)
            entity = map_to
        if weighted:
            series = MicroSeries(
                arr,
                weights=self.calc(
                    f"{entity}_weight", period=period, weighted=False
                ),
            )
            return series
        else:
            return arr

    def df(self, variables, period=2020):
        df_dict = {}
        var_metadata = self.simulation.tax_benefit_system.variables[
            variables[0]
        ]
        entity = var_metadata.entity.key
        weights = self.calc(f"{entity}_weight", period=period, weighted=False)
        for var in variables:
            df_dict[var] = self.calc(var, period=period)
        return MicroDataFrame(df_dict, weights=weights)

    def deriv(
        self,
        target="tax",
        wrt="employment_income",
        delta=100,
        percent=False,
        group_limit=2,
    ) -> MicroSeries:
        """Calculates effective marginal tax rates over a population.

        Args:
            targets (str, optional): The name of the variable to measure the derivative of. Defaults to "household_net_income".
            wrt (str, optional): The name of the independent variable. Defaults to "employment_income".

        Returns:
            np.array: [description]
        """
        system = self.simulation.tax_benefit_system
        target_entity = system.variables[target].entity.key
        wrt_entity = system.variables[wrt].entity.key
        if target_entity == wrt_entity:
            # calculating a derivative with both source and target in the same entity
            config = (wrt, delta, percent, "same-entity")
            if config not in self.bonus_sims:
                existing_var_class = system.variables[wrt].__class__

                altered_variable = type(wrt, (existing_var_class,), {})
                if not percent:
                    altered_variable.formula = (
                        lambda *args: existing_var_class.formula(*args) + delta
                    )
                else:
                    altered_variable.formula = lambda *args: existing_var_class.formula(
                        *args
                    ) * (
                        1.0 + delta / 100
                    )

                class bonus_ref(Reform):
                    def apply(self):
                        self.update_variable(altered_variable)

                self.bonus_sims[config] = Microsimulation(
                    self.reforms[1:] + (bonus_ref,),
                )
            bonus_sim = self.bonus_sims[config]
            bonus_increase = bonus_sim.calc(wrt).astype(float) - self.calc(
                wrt
            ).astype(float)
            target_increase = bonus_sim.calc(target).astype(float) - self.calc(
                target
            ).astype(float)

            gradient = target_increase / bonus_increase

            return gradient
        elif target_entity in ("taxunit",) and wrt_entity == "person":
            # calculate the derivative for a group variable wrt a source variable, independent of other members in the group
            adult = True
            index_in_group = (
                self.calc("person_id")
                .groupby(self.calc(f"{target_entity}_id", map_to="person"))
                .cumcount()
            )
            max_group_size = min(max(index_in_group[adult]) + 1, group_limit)

            derivative = np.empty((len(adult))) * np.nan

            for i in range(
                max_group_size, desc="Calculating independent derivatives"
            ):
                config = (wrt, delta, percent, "group-entity", i)
                if config not in self.bonus_sims:
                    existing_var_class = system.variables[wrt].__class__

                    altered_variable = type(wrt, (existing_var_class,), {})
                    if not percent:
                        altered_variable.formula = (
                            lambda person, *args: existing_var_class.formula(
                                person, *args
                            )
                            + delta * (index_in_group == i) * adult
                        )
                    else:
                        delta /= 100
                        altered_variable.formula = lambda *args: existing_var_class.formula(
                            *args
                        ) * (
                            1.0 + delta * (index_in_group == i) * adult
                        )

                    class bonus_ref(Reform):
                        def apply(self):
                            self.update_variable(altered_variable)

                    self.bonus_sims[config] = Microsimulation(
                        self.reforms[1:] + (bonus_ref,),
                        mode=self.mode,
                        year=self.year,
                        input_year=self.input_year,
                    )
                bonus_sim = self.bonus_sims[config]
                bonus_increase = bonus_sim.calc(wrt).astype(float) - self.calc(
                    wrt
                ).astype(float)
                target_increase = bonus_sim.calc(
                    target, map_to="person"
                ).astype(float) - self.calc(target, map_to="person").astype(
                    float
                )
                result = target_increase / bonus_increase
                derivative[bonus_increase > 0] = result[bonus_increase > 0]

            return MicroSeries(
                derivative, weights=self.entity_weights["person"]
            )
        else:
            raise ValueError(
                "Unable to compute derivative - target variable must be from a group of or the same as the source variable"
            )
