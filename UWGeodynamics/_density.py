import underworld.function as fn
from .scaling import nonDimensionalize as nd
from .scaling import UnitRegistry as u


class Density(object):

    def __init__(self, temperatureField, pressureField):
        self.temperatureField = temperatureField
        self.pressureField = pressureField
        self.name = None


class ConstantDensity(Density):

    def __init__(self, density):
        self.reference_density = density
        self._density = nd(density)
        self.name = "Constant ({0})".format(str(density))

    def effective_density(self):
        return fn.misc.constant(self._density)

    def to_json(self):
        d = {}
        d["type"] = "ConstantDensity"
        d["reference_density"] = self.reference_density


class LinearDensity(Density):

    def __init__(self, reference_density, thermalExpansivity=3e-5 / u.kelvin,
                 reference_temperature=273.15 * u.degK, beta=0. / u.pascal,
                 reference_pressure=0. * u.pascal, temperatureField=None,
                 pressureField=None):

        """ The LinearDensity function calculates:
            density = rho0 * (1 + (beta * deltaP) - (alpha * deltaT))
            where deltaP is the difference between P and the reference P, and deltaT
            is the difference between T and the reference T."""

        super(LinearDensity, self).__init__(temperatureField,
                                            pressureField)

        self.name = "Linear (ref: {0})".format(str(reference_density))
        self.reference_density = reference_density
        self.reference_temperature = reference_temperature
        self.thermalExpansivity = thermalExpansivity
        self.reference_pressure = reference_pressure
        self._alpha = nd(thermalExpansivity)
        self._beta = nd(beta)
        self._Tref = nd(reference_temperature)
        self._Pref = nd(reference_pressure)

    def to_json(self):
        d = {}
        d["type"] = "LinearDensity"
        d["reference_density"] = str(self.reference_density)
        d["thermalExpansivity"] =  str(self.thermalExpansivity)
        d["reference_temperature"] = str(self.reference_temperature)
        d["beta"] = str(self._beta)
        d["reference_density"] = str(self.reference_pressure)
        return d

    def effective_density(self):

        density = nd(self.reference_density)

        # Temperature dependency
        if not self.temperatureField:
            raise RuntimeError("No temperatureField found!")

        t_term = self._alpha * (self.temperatureField - self._Tref)

        # Pressure dependency
        if not self.pressureField:
            raise RuntimeError("No pressureField found!")

        p_term = self._beta * (self.pressureField - self._Pref)

        return density * (1.0 + p_term - t_term)

