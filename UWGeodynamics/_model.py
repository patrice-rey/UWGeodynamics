from __future__ import print_function, absolute_import
import os
import sys
from collections import OrderedDict
import numpy as np
import underworld as uw
import underworld.function as fn
from underworld.function.exception import SafeMaths as Safe
import UWGeodynamics.shapes as shapes
import UWGeodynamics.surfaceProcesses as surfaceProcesses
from . import rcParams
from .scaling import Dimensionalize
from .scaling import nonDimensionalize as nd
from .scaling import UnitRegistry as u
from .lithopress import LithostaticPressure
from ._utils import PressureSmoother, PassiveTracers, PassiveTracersGrid
from ._rheology import ViscosityLimiter, StressLimiter
from ._material import Material
from ._visugrid import Visugrid
from ._velocity_boundaries import VelocityBCs
from ._velocity_boundaries import StressBCs
from ._thermal_boundaries import TemperatureBCs
from ._mesh_advector import _mesh_advector
from ._frictional_boundary import FrictionBoundaries
from .Underworld_extended import FeMesh_Cartesian
from .Underworld_extended import Swarm
from .Underworld_extended import MeshVariable
from .Underworld_extended import SwarmVariable
from datetime import datetime
from .version import full_version
from ._freesurface import FreeSurfaceProcessor
from mpi4py import MPI

_dim_gravity = {'[length]': 1.0, '[time]': -2.0}
_dim_time = {'[time]': 1.0}


class Model(Material):
    """UWGeodynamic Model Class"""

    @u.check([None, (None, None), ("[length]", "[length]"), None,
              _dim_gravity])
    def __init__(self, elementRes=(64, 64),
                 minCoord=(0., 0.), maxCoord=(64. * u.kilometer, 64 * u.kilometer),
                 name=None, gravity=None, periodic=None, elementType=None,
                 temperatureBCs=None, velocityBCs=None, stressBCs=None, materials=None,
                 outputDir=None, frictionalBCs=None, surfaceProcesses=None,
                 isostasy=None, visugrid=None):
        """__init__

        Parameters
        ----------

        elementRes :
            Resolution of the mesh in number of elements for each axis (degree
            of freedom)
        minCoord :
            Minimum coordinates for each axis.
        maxCoord :
            Maximum coordinates for each axis.
        name :
            The Model name.
        gravity :
            Acceleration due to gravity vector.
        periodic :
            Mesh periodicity.
        elementType :
            Type of finite element.
        temperatureBCs :
            Temperature Boundary Condition, must be object of type:
            TemperatureBCs
        velocityBCs :
            Velocity Boundary Condtion, must be object of type VelocityBCs
        stressBCs :
            Stress Boundary Condtion, must be object of type StressBCs
        materials :
            List of materials, each material must be an object of type:
            Material
        outputDir :
            Output Directory
        frictionalBCs :
            Frictional Boundary Conditions, must be object of type:
            FrictionBoundaries
        surfaceProcesses :
            Surface Processes, must be an object of type:
            surfaceProcesses
        isostasy :
            Isostasy Solver
        visugrid :
            Visugrid object
        advector :
            Mesh advector object

        Examples
        --------
        >>> import UWGeodynamics as GEO
        >>> u = GEO.UnitRegistry
        >>> Model = Model = GEO.Model(
                elementRes=(64, 64),
                minCoord=(0., 0.),
                maxCoord=(64. * u.kilometer, 64. * u.kilometer))

        """

        super(Model, self).__init__()

        # Process __init__ arguments
        if not name:
            self.name = rcParams["model.name"]
        else:
            self.name = name

        self.minCoord = minCoord
        self.maxCoord = maxCoord
        self.top = maxCoord[-1]
        self.bottom = minCoord[-1]
        self.dimension = dim = len(maxCoord)

        if not gravity:
            self.gravity = [0.0 for val in range(self.dimension)]
            self.gravity[-1] = -1.0 * rcParams["gravity"]
            self.gravity = tuple(self.gravity)
        else:
            self.gravity = gravity

        if not elementType:
            self.elementType = rcParams["element.type"]
        else:
            self.elementType = elementType

        self.elementRes = elementRes

        if outputDir:
            self.outputDir = outputDir
        else:
            self.outputDir = rcParams["output.directory"]

        # Compute model dimensions
        self.length = maxCoord[0] - minCoord[0]
        self.height = maxCoord[-1] - minCoord[-1]

        if dim == 3:
            self.width = maxCoord[1] - minCoord[1]

        if periodic:
            self.periodic = periodic
        else:
            periodic = tuple([False for val in range(dim)])
            self.periodic = periodic

        # Get non-dimensional extents along each axis
        minCoord = tuple([nd(val) for val in self.minCoord])
        maxCoord = tuple([nd(val) for val in self.maxCoord])

        # Initialize model mesh
        self.mesh = FeMesh_Cartesian(elementType=self.elementType,
                                     elementRes=self.elementRes,
                                     minCoord=minCoord,
                                     maxCoord=maxCoord,
                                     periodic=self.periodic)

        self.mesh_fields = {}
        self.submesh_fields = {}
        self.swarm_fields = {}

        # Add common mesh variables
        self.temperature = False
        self.add_submesh_field("pressureField", nodeDofCount=1)
        self.add_mesh_field("velocityField", nodeDofCount=self.mesh.dim)
        self.add_mesh_field("tractionField", nodeDofCount=self.mesh.dim)
        self.add_submesh_field("_strainRateField", nodeDofCount=1)

        # Create a field to check applied boundary conditions
        self.add_mesh_field("boundariesField", nodeDofCount=self.mesh.dim)

        # symmetric component of the gradient of the flow velocityField.
        self.strainRate = fn.tensor.symmetric(self.velocityField.fn_gradient)
        self._strainRate_2ndInvariant = None

        # Create the material swarm
        self.swarm = Swarm(mesh=self.mesh, particleEscape=True)
        if self.mesh.dim == 2:
            particlesPerCell = rcParams["swarm.particles.per.cell.2D"]
        else:
            particlesPerCell = rcParams["swarm.particles.per.cell.3D"]

        self._swarmLayout = uw.swarm.layouts.PerCellSpaceFillerLayout(
            swarm=self.swarm,
            particlesPerCell=particlesPerCell)

        self.swarm.populate_using_layout(layout=self._swarmLayout)

        # timing and checkpointing
        self.checkpointID = 0
        self.time = 0.0 * u.megayears
        self.step = 0
        self._dt = None

        self._defaultMaterial = self.index
        self.materials = list(materials) if materials is not None else list()
        self.materials.append(self)

        # Create a series of aliases for the boundary sets
        self.left_wall = self.mesh.specialSets["MinI_VertexSet"]
        self.right_wall = self.mesh.specialSets["MaxI_VertexSet"]

        if self.mesh.dim == 2:
            self.top_wall = self.mesh.specialSets["MaxJ_VertexSet"]
            self.bottom_wall = self.mesh.specialSets["MinJ_VertexSet"]
        else:
            self.front_wall = self.mesh.specialSets["MinJ_VertexSet"]
            self.back_wall = self.mesh.specialSets["MaxJ_VertexSet"]
            self.top_wall = self.mesh.specialSets["MaxK_VertexSet"]
            self.bottom_wall = self.mesh.specialSets["MinK_VertexSet"]

        # Boundary Conditions
        self.velocityBCs = velocityBCs
        self.stressBCs = stressBCs
        self.temperatureBCs = temperatureBCs
        self.frictionalBCs = frictionalBCs
        self._isostasy = isostasy
        self.surfaceProcesses = surfaceProcesses

        self.pressSmoother = PressureSmoother(self.mesh, self.pressureField)

        # Passive Tracers
        # An ordered dict is required to ensure that all threads process
        # the same swarm at a time.
        self.passive_tracers = OrderedDict()

        # Visualisation
        self._visugrid = visugrid

        # Mesh advector
        self._advector = None

        # init solver
        self._solver = None
        self._static_solver = False

        # Initialise remaining attributes
        self.defaultStrainRate = 1e-15 / u.second
        self._solution_exist = fn.misc.constant(False)
        self._isYielding = None
        self._temperatureDot = None
        self._temperature = None
        self.DiffusivityFn = None
        self.HeatProdFn = None
        self._buoyancyFn = None
        self._freeSurface = False
        self.callback_post_solve = None
        self._mesh_saved = False
        self._initialize()

    def _initialize(self):
        """_initialize
        Model Initialisation
        """

        self.swarm_advector = uw.systems.SwarmAdvector(
            swarm=self.swarm,
            velocityField=self.velocityField,
            order=2
        )

        if self.mesh.dim == 2:
            particlesPerCell = rcParams["popcontrol.particles.per.cell.2D"]
        else:
            particlesPerCell = rcParams["popcontrol.particles.per.cell.3D"]

        self.population_control = uw.swarm.PopulationControl(
            self.swarm,
            aggressive=rcParams["popcontrol.aggressive"],
            splitThreshold=rcParams["popcontrol.split.threshold"],
            maxSplits=rcParams["popcontrol.max.splits"],
            particlesPerCell=particlesPerCell)

        # Add Common Swarm Variables
        self.add_swarm_field("materialField", dataType="int", count=1,
                             init_value=self.index)
        self.add_swarm_field("plasticStrain", dataType="double", count=1)
        self.add_swarm_field("_viscosityField", dataType="double", count=1)
        self.add_swarm_field("_densityField", dataType="double", count=1)
        self.add_swarm_field("meltField", dataType="double", count=1)
        self.add_swarm_field("timeField", dataType="double", count=1)
        self.timeField.data[...] = 0.0
        self.materialField.data[...] = self.index

        if self.mesh.dim == 3:
            stress_dim = 6
        else:
            stress_dim = 3

        self.add_swarm_field("_previousStressField", dataType="double",
                             count=stress_dim)
        self.add_swarm_field("_stressTensor", dataType="double",
                             count=stress_dim, projected="submesh")
        self.add_swarm_field("_stressField", dataType="double",
                             count=1, projected="submesh")

    def __getitem__(self, name):
        """__getitem__

        Return item with name=name from the class __dict__
        This allows the user to get the attributes of the model
        class as:
            Model["name"]

        Parameters
        ----------

        name : name of the attribute

        Returns
        -------
        Attribute of the Model instance.
        self.__dict__[name]
        """
        return self.__dict__[name]

    def _repr_html_(self):
        """_repr_html_

        HTML table describing the model.
        For integration with Jupyter notebook.
        """
        return _model_html_repr(self)

    @property
    def x(self):
        """x"""
        return fn.input()[0]

    @property
    def y(self):
        """y"""
        return fn.input()[1]

    @property
    def z(self):
        """z"""
        return fn.input()[2]

    @property
    def outputDir(self):
        """ Output Directory """
        return self._outputDir

    @outputDir.setter
    def outputDir(self, value):
        self._outputDir = value

    def restart(self, step, restartDir):
        """restart

        Parameters
        ----------

        step :
                Step from which you want to restart the model.
                Must be an int (step number either absolute or relative)
                if step == -1, run the last available step
                if step == -2, run the second last etc.
        restartDir :
                Directory that contains the outputs of the model
                you want to restart from.

        Returns
        -------

        This function returns None
        """

        if not isinstance(step, int):
            raise ValueError("step must be an int or a bool")

        if not os.path.exists(restartDir):
            raise ValueError("restartDir must be a path to an existing folder")

        # Do not raise and error idf directory is empty, just run
        # the model...
        if not os.listdir(restartDir):
            return

        # Look for step with swarm available
        indices = [int(os.path.splitext(filename)[0].split("-")[-1])
                   for filename in os.listdir(restartDir) if "-" in
                   filename]
        indices.sort()

        if step < 0:
            step = indices[step]

        if step not in indices:
            raise ValueError("Cannot find step in specified folder")

        # Ready for restart

        # Get time from swarm-%.h5 file
        import h5py
        with h5py.File(os.path.join(restartDir, "swarm-%s.h5" % step), "r",
                       driver="mpio", comm=MPI.COMM_WORLD) as h5f:
            self.time = u.Quantity(h5f.attrs.get("time"))

        if uw.rank() == 0:
            print(80 * "=" + "\n")
            print("Restarting Model from Step {0} at Time = {1}\n".format(step,self.time))
            print('(' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ')')
            print(80 * "=" + "\n")
            sys.stdout.flush()

        self.checkpointID = step

        # Reload deformed mesh if advector is present
        if self._advector:
            self.mesh.load(os.path.join(restartDir, 'mesh-%s.h5' % step))
        # Load mesh
        else:
            self.mesh.load(os.path.join(restartDir, "mesh.h5"))

        if uw.rank() == 0:
            print("Mesh loaded" + '(' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ')')
            sys.stdout.flush()

        self.swarm = Swarm(mesh=self.mesh, particleEscape=True)
        self.swarm.load(os.path.join(restartDir, 'swarm-%s.h5' % step))

        if uw.rank() == 0:
            print("Swarm loaded" + '(' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ')')
            sys.stdout.flush()

        self._initialize()

        # Reload all the restart fields
        for field in rcParams["restart.fields"]:
            if field == "temperature":
                continue
            obj = getattr(self, field)
            path = os.path.join(restartDir, field + "-%s.h5" % step)
            if uw.rank() == 0:
                print("Reloading field {0} from {1}".format(field, path))
                sys.stdout.flush()
            obj.load(str(path))
            if uw.rank() == 0:
                print("{0} loaded".format(field) + '(' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ')')
                sys.stdout.flush()

        # Temperature is a special case...
        path = os.path.join(restartDir, "temperature" + "-%s.h5" % step)
        if os.path.exists(path):
            if not self.temperature:
                self.temperature = True
            obj = getattr(self, "temperature")
            if uw.rank() == 0:
                print("Reloading field {0} from {1}".format("temperature", path))
                sys.stdout.flush()
            obj.load(str(path))
            if uw.rank() == 0:
                print("Temperature loaded" + '(' +
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ')')
                sys.stdout.flush()

        # Reload Passive Tracers
        for key, tracer in self.passive_tracers.items():

            if uw.rank() == 0:
                print("Reloading {0} passive tracers".format(tracer.name))
                sys.stdout.flush()

            fname = tracer.name + '-%s.h5' % step
            fpath = os.path.join(restartDir, fname)
            with h5py.File(fpath, "r", driver="mpio", comm=MPI.COMM_WORLD) as h5f:
                vertices = h5f["data"].value * u.Quantity(h5f.attrs["units"])
                vertices = [vertices[:, dim] for dim in range(self.mesh.dim)]
                obj = PassiveTracers(self.mesh,
                                     self.velocityField,
                                     tracer.name,
                                     vertices=vertices,
                                     particleEscape=tracer.particleEscape)

            attr_name = tracer.name.lower() + "_tracers"
            setattr(self, attr_name, obj)
            self.passive_tracers[key] = obj
            if uw.rank() == 0:
                print("{0} loaded".format(tracer.name) + '(' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ')')
                sys.stdout.flush()

        if isinstance(self.surfaceProcesses,
                      (surfaceProcesses.SedimentationThreshold,
                       surfaceProcesses.ErosionThreshold,
                       surfaceProcesses.ErosionAndSedimentationThreshold)):

            obj = self.surfaceProcesses
            obj.Model = self
            obj.timeField = self.timeField

        # Restart Badlands if we are running a coupled model
        if isinstance(self.surfaceProcesses, surfaceProcesses.Badlands):
            badlands_model = self.surfaceProcesses
            restartFolder = badlands_model.restartFolder
            restartStep = badlands_model.restartStep

            # Parse xmf for the last timestep time
            import xml.etree.ElementTree as etree
            xmf = restartFolder + "/xmf/tin.time" + str(restartStep) + ".xmf"
            tree = etree.parse(xmf)
            root = tree.getroot()
            badlands_time = float(root[0][0][0].attrib["Value"])
            uw_time = self.time.to(u.years).magnitude

            if np.abs(badlands_time - uw_time) > 1:
                raise ValueError("""Time in Underworld and Badlands outputs
                                 differs:\n
                                 Badlands: {0}\n
                                 Underworld: {1}""".format(badlands_time,
                                                           uw_time))

            airIndex = badlands_model.airIndex
            sedimentIndex = badlands_model.sedimentIndex
            XML = badlands_model.XML
            resolution = badlands_model.resolution
            checkpoint_interval = badlands_model.checkpoint_interval

            self.surfaceProcesses = surfaceProcesses.Badlands(
                airIndex, sedimentIndex,
                XML, resolution,
                checkpoint_interval,
                restartFolder=restartFolder,
                restartStep=restartStep)
            if uw.rank() == 0:
                print("Badlands restarted" + '(' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ')')
                sys.stdout.flush()

        return

    @property
    def projMaterialField(self):
        """ Material field projected on the mesh """
        self._materialFieldProjector.solve()
        self._projMaterialField.data[:] = np.rint(
            self._projMaterialField.data[:]
        )
        return self._projMaterialField

    @property
    def projPlasticStrain(self):
        """ Plastic Strain Field projected on the mesh """
        self._plasticStrainProjector.solve()
        return self._projPlasticStrain

    @property
    def projTimeField(self):
        """ Time Field projected on the mesh """
        self._timeFieldProjector.solve()
        return self._projTimeField

    @property
    def projMeltField(self):
        """ Melt Field projected on the mesh """
        self._meltFieldProjector.solve()
        return self._projMeltField

    @property
    def strainRate_2ndInvariant(self):
        """ Strain Rate Field """
        self._strainRate_2ndInvariant = fn.tensor.second_invariant(
            self.strainRate
        )
        condition = [(self._solution_exist, self._strainRate_2ndInvariant),
                     (True, fn.misc.constant(nd(self.defaultStrainRate)))]
        self._strainRate_2ndInvariant = fn.branching.conditional(condition)
        return self._strainRate_2ndInvariant

    @property
    def strainRateField(self):
        self._strainRateField.data[:] = (
            self.strainRate_2ndInvariant.evaluate(self.mesh.subMesh))
        return self._strainRateField

    @property
    def projViscosityField(self):
        """ Viscosity Field projected on the mesh """
        self.viscosityField.data[...] = self._viscosityFn.evaluate(self.swarm)
        self._viscosityFieldProjector.solve()
        return self._projViscosityField

    @property
    def viscosityField(self):
        """ Viscosity Field on particles """
        self._viscosityField.data[:] = self._viscosityFn.evaluate(self.swarm)
        return self._viscosityField

    @property
    def projStressTensor(self):
        """ Stress Tensor on mesh """
        self._stressTensor.data[...] = self._stressFn.evaluate(self.swarm)
        self._stressTensorProjector.solve()
        return self._projStressTensor

    @property
    def projStressField(self):
        """ Second Invariant of the Stress tensor projected on the submesh"""
        stress = fn.tensor.second_invariant(self._stressFn)
        self._stressField.data[...] = stress.evaluate(self.swarm)
        self._stressFieldProjector.solve()
        return self._projStressField

    @property
    def projDensityField(self):
        """ Density Field projected on the mesh """
        self.densityField.data[...] = self._densityFn.evaluate(self.swarm)
        self._densityFieldProjector.solve()
        return self._projDensityField

    @property
    def densityField(self):
        """ Density Field on particles """
        self._densityField.data[:] = self._densityFn.evaluate(self.swarm)
        return self._densityField

    @property
    def surfaceProcesses(self):
        """ Surface processes handler """
        return self._surfaceProcesses

    @surfaceProcesses.setter
    def surfaceProcesses(self, value):
        self._surfaceProcesses = value
        if value:
            self._surfaceProcesses.timeField = self.timeField
            self._surfaceProcesses.Model = self

    def set_temperatureBCs(self, left=None, right=None,
                           top=None, bottom=None,
                           front=None, back=None,
                           nodeSets=None, materials=None,
                           bottom_material=None, top_material=None,
                           left_material=None, right_material=None,
                           back_material=None, front_material=None):

        """ Set Model thermal conditions

        Parameters:
            left:
                Temperature or flux along the left wall.
                Flux must be a vector (Fx, Fy, [Fz])
                Default is 'None'
            right:
                Temperature or flux along the right wall.
                Flux must be a vector (Fx, Fy, [Fz])
                Default is 'None'
            top:
                Temperature or flux along the top wall.
                Flux must be a vector (Fx, Fy, [Fz])
                Default is 'None'
            bottom:
                Temperature or flux along the bottom wall.
                Flux must be a vector (Fx, Fy, [Fz])
                Default is 'None'
            front:
                Temperature or flux along the front wall.
                Flux must be a vector (Fx, Fy, [Fz])
                Default is 'None'
            back:
                Temperature or flux along the back wall.
                Flux must be a vector (Fx, Fy, [Fz])
                Default is 'None'
            nodeSets: (set, temperature)
                A list, numpy array which contains the indices (global)
                of the nodes and the associate temperature.
            materials:
                list of materials for which temperature need to be
                fixed. [(material, temperature)]
            bottom_material:
                Specify Material that lays at the bottom of the model
                Default is 'None' (required if a flux is defined at the
                base).
            top_material:
                Specify Material that lays at the top of the model
                Default is 'None' (required if a flux is defined at the top).
            left_material:
                Specify Material that lays at the left of the model
                Default is 'None' (required if a flux is defined at the left).
            right_material:
                Specify Material that lays at the right of the model
                Default is 'None' (required if a flux is defined at the right).
            back_material:
                Specify Material that lays at the back of the model
                Default is 'None' (required if a flux is defined at the back).
            front_material:
                Specify Material that lays at the front of the model
                Default is 'None' (required if a flux is defined at the front).

        """

        if not self.temperature:
            self.temperature = True

        self.temperatureBCs = TemperatureBCs(self, left=left, right=right,
                                             top=top, bottom=bottom,
                                             back=back, front=front,
                                             nodeSets=nodeSets,
                                             materials=materials,
                                             bottom_material=bottom_material,
                                             top_material=top_material,
                                             left_material=left_material,
                                             right_material=right_material,
                                             back_material=back_material,
                                             front_material=front_material)
        return self.temperatureBCs.get_conditions()

    @property
    def solver(self):
        return self._solver

    @solver.setter
    def solver(self, value):
        self._static_solver = True
        self._solver = value

    @property
    def temperature(self):
        """ Temperature Field """
        return self._temperature

    @temperature.setter
    def temperature(self, value):
        if value is True:
            self._temperature = MeshVariable(mesh=self.mesh,
                                                nodeDofCount=1)
            self._temperatureDot = MeshVariable(mesh=self.mesh,
                                                    nodeDofCount=1)
            self._heatFlux = MeshVariable(mesh=self.mesh,
                                              nodeDofCount=1)
            self._temperatureDot.data[...] = 0.
            self._heatFlux.data[...] = 0.
        else:
            self._temperature = False

    @property
    def _temperatureBCs(self):
        if not self.temperatureBCs:
            raise ValueError("Set Boundary Conditions")
        return self.temperatureBCs.get_conditions()

    @property
    def _advdiffSystem(self):
        """ Advection Diffusion System """

        DiffusivityMap = {}
        for material in self.materials:
            if material.diffusivity:
                DiffusivityMap[material.index] = nd(material.diffusivity)

        self.DiffusivityFn = fn.branching.map(fn_key=self.materialField,
                                              mapping=DiffusivityMap,
                                              fn_default=nd(self.diffusivity))

        HeatProdMap = {}
        for material in self.materials:
            if all([material.density,
                    material.capacity,
                    material.radiogenicHeatProd]):

                HeatProdMap[material.index] = (
                    nd(material.radiogenicHeatProd) /
                    (self._densityFn * nd(material.capacity)))
            else:
                HeatProdMap[material.index] = 0.

        self.HeatProdFn = fn.branching.map(fn_key=self.materialField,
                                           mapping=HeatProdMap)

        # Add Viscous dissipation Heating
        if rcParams["shearHeating"]:
            stress = fn.tensor.second_invariant(self._stressFn)
            strain = self.strainRate_2ndInvariant
            self.HeatProdFn += stress * strain

        if rcParams["advection.diffusion.method"] is "SLCN":

            obj = uw.systems.SLCN_AdvectionDiffusion(
                self.temperature,
                velocityField=self.velocityField,
                fn_diffusivity=self.DiffusivityFn,
                fn_sourceTerm=self.HeatProdFn,
                conditions=self._temperatureBCs
            )

        if rcParams["advection.diffusion.method"] is "SUPG":

            obj = uw.systems.AdvectionDiffusion(
                self.temperature,
                self._temperatureDot,
                velocityField=self.velocityField,
                fn_diffusivity=self.DiffusivityFn,
                fn_sourceTerm=self.HeatProdFn,
                conditions=self._temperatureBCs
            )
        return obj

    def get_stokes_solver(self):
        """ Stokes solver """

        if not self._solver or not self._static_solver:
            gravity = tuple([nd(val) for val in self.gravity])
            self._buoyancyFn = self._densityFn * gravity
            self._buoyancyFn = self._buoyancyFn

            if any([material.viscosity for material in self.materials]):

                conditions = list()
                conditions.append(self.velocityBCs.get_conditions())

                if self.stressBCs:
                    conditions.append(self.stressBCs.get_conditions())

                self._stokes_SLE = uw.systems.Stokes(
                    velocityField=self.velocityField,
                    pressureField=self.pressureField,
                    conditions=conditions,
                    fn_viscosity=self._viscosityFn,
                    fn_bodyforce=self._buoyancyFn,
                    fn_stresshistory=self._elastic_stressFn,
                    fn_one_on_lambda=self._lambdaFn)
                    #useEquationResidual=rcParams["useEquationResidual"])

                self._solver = uw.systems.Solver(self._stokes_SLE)
                self._solver.set_inner_method(rcParams["solver"])

                if rcParams["penalty"]:
                    self._solver.set_penalty(rcParams["penalty"])

                if rcParams["mg.levels"]:
                    self._solver.options.mg.levels = rcParams["mg.levels"]

            if self._solver._check_linearity(False):
                if not hasattr(self, "prevVelocityField"):
                    self.add_mesh_field("prevVelocityField", nodeDofCount=self.mesh.dim)
                if not hasattr(self, "prevPressureField"):
                    self.add_submesh_field("prevPressureField", nodeDofCount=1)

        return self._solver

    def _init_melt_fraction(self):
        """ Initialize the Melt Fraction Field """

        # Initialize Melt Fraction to material property
        meltFractionMap = {}
        for material in self.materials:
            if material.meltFraction:
                meltFractionMap[material.index] = material.meltFraction

        if meltFractionMap:
            InitFn = fn.branching.map(fn_key=self.materialField,
                                      mapping=meltFractionMap, fn_default=0.0)
            self.meltField.data[:] = InitFn.evaluate(self.swarm)

    def set_velocityBCs(self, left=None, right=None, top=None, bottom=None,
                        front=None, back=None, nodeSets=None,
                        order_wall_conditions=None):
        """ Set Model kinematic conditions

        Parameters:
            left:
                Velocity along the left wall.
                Default is 'None'
            right:
                Velocity along the right wall.
                Default is 'None'
            top:
                Velocity along the top wall.
                Default is 'None'
            bottom:
                Velocity along the bottom wall.
                Default is 'None'
            front:
                Velocity along the front wall.
                Default is 'None'
            back:
                Velocity along the back wall.
                Default is 'None'
            nodeSets: (set, velocity)
                underworld mesh index set and associate velocity
        """

        self.velocityBCs = VelocityBCs(self, left=left,
                                        right=right, top=top,
                                        bottom=bottom, front=front,
                                        back=back, nodeSets=nodeSets,
                                        order_wall_conditions=order_wall_conditions)
        return self.velocityBCs

    set_kinematicBCs = set_velocityBCs

    def set_stressBCs(self, left=None, right=None, top=None, bottom=None,
                      front=None, back=None, nodeSets=None,
                      order_wall_conditions=None):
        """ Set Model kinematic conditions

        Parameters:
            left:
                Velocity along the left wall.
                Default is 'None'
            right:
                Velocity along the right wall.
                Default is 'None'
            top:
                Velocity along the top wall.
                Default is 'None'
            bottom:
                Velocity along the bottom wall.
                Default is 'None'
            front:
                Velocity along the front wall.
                Default is 'None'
            back:
                Velocity along the back wall.
                Default is 'None'
            nodeSets: (set, velocity)
                underworld mesh index set and associate velocity
        """

        self.stressBCs = StressBCs(self, left=left,
                                   right=right, top=top,
                                   bottom=bottom, front=front,
                                   back=back, nodeSets=nodeSets,
                                   order_wall_conditions=order_wall_conditions)
        return self.stressBCs

    def add_material(self, shape=None, name="unknown", fill=True, reset=False):
        """ Add Material to the Model

        Parameters:
        -----------
            material:
                An UWGeodynamics material. If None the material is
                initialized to the global properties.
            shape:
                Shape of the material. See UWGeodynamics.shape
            name:
                Material name
            reset: (bool)
                Reset the material Field before adding the new
                material. Default is False.

        """

        if reset:
            self.materialField.data[:] = self.index

        mat = Material()
        mat.name = name
        mat.Model = self
        mat.diffusivity = self.diffusivity
        mat.capacity = self.capacity
        mat.radiogenicHeatProd = self.radiogenicHeatProd

        if isinstance(shape, shapes.Layer):
            if self.mesh.dim == 2:
                shape = shapes.Layer2D(top=shape.top, bottom=shape.bottom)
            if self.mesh.dim == 3:
                shape = shapes.Layer3D(top=shape.top, bottom=shape.bottom)

        if hasattr(shape, "top"):
            mat.top = shape.top
        if hasattr(shape, "bottom"):
            mat.bottom = shape.bottom

        mat.shape = shape
        self.materials.reverse()
        self.materials.append(mat)
        self.materials.reverse()

        if mat.shape:
            if isinstance(mat.shape, shapes.Shape):
                condition = [(mat.shape.fn, mat.index), (True, self.materialField)]
            elif isinstance(mat.shape, uw.function.Function):
                condition = [(mat.shape, mat.index), (True, self.materialField)]
            func = fn.branching.conditional(condition)
            self.materialField.data[:] = func.evaluate(self.swarm)

        return mat

    def add_swarm_field(self, name, dataType="double", count=1,
                        init_value=0., projected="mesh", **kwargs):
        """Add a new swarm field to the model

        Parameters
        ----------

        name : name of the swarm field
        dataType : type of data to be recorded, default is "double"
        count : degree of freedom, default is 1
        init_value : default value of the field, default is to initialise
            the field to 0.
        projected : the function creates a projector for each new
            swarm variable, you can choose to project on the "mesh" or
            "submesh"

        Returns
        -------

        Swarm Variable
        """

        newField = self.swarm.add_variable(dataType, count, **kwargs)
        setattr(self, name, newField)
        newField.data[...] = init_value
        self.swarm_fields[name] = newField

        # Create mesh variable for projection
        if name.startswith("_"):
            proj_name = "_proj" + name[1].upper() + name[2:]
        else:
            proj_name = "_proj" + name[0].upper() + name[1:]

        if projected == "mesh":
            projected = self.add_mesh_field(proj_name,
                                            nodeDofCount=count,
                                            dataType="double")
        else:
            projected = self.add_submesh_field(proj_name,
                                               nodeDofCount=count,
                                               dataType="double")

        # Create a projector
        if name.startswith("_"):
            projector_name = name + "Projector"
        else:
            projector_name = "_" + name + "Projector"
        projector = uw.utils.MeshVariable_Projection(projected,
                                                     newField,
                                                     voronoi_swarm=self.swarm,
                                                     type=0)
        setattr(self, projector_name, projector)

        return newField

    def add_mesh_field(self, name, nodeDofCount=1,
                       dataType="double", init_value=0., **kwargs):
        """Add a new mesh field to the model

        Parameters
        ----------

        name : name of the mesh field
        nodeDofCount : degree of freedom, default is 1
        dataType : type of data to be recorded, default is "double"
        init_value : default value of the field, default is to initialise
            the field to 0.

        Returns
        -------
        Mesh Variable
        """
        newField = self.mesh.add_variable(nodeDofCount, dataType, **kwargs)
        setattr(self, name, newField)
        newField.data[...] = init_value
        self.mesh_fields[name] = newField
        return newField

    def add_submesh_field(self, name, nodeDofCount=1,
                          dataType="double", init_value=0., **kwargs):
        """Add a new sub-mesh field to the model

        Parameters
        ----------

        name : name of the submesh field
        nodeDofCount : degree of freedom, default is 1
        dataType : type of data to be recorded, default is "double"
        init_value : default value of the field, default is to initialise
            the field to 0.

        Returns
        -------
        Mesh Variable
        """
        newField = MeshVariable(self.mesh.subMesh, nodeDofCount,
                                dataType, **kwargs)
        setattr(self, name, newField)
        newField.data[...] = init_value
        self.submesh_fields[name] = newField
        return newField

    @property
    def _densityFn(self):
        """Density Function Builder"""
        densityMap = {}
        for material in self.materials:

            if self.temperature:
                dens_handler = material.density
                dens_handler.temperatureField = self.temperature
                dens_handler.pressureField = self.pressureField
                densityMap[material.index] = dens_handler.effective_density()
            else:
                dens_handler = material.density
                densityMap[material.index] = nd(dens_handler.reference_density)

            if material.meltExpansion:
                fact = material.meltExpansion * self.meltField
                densityMap[material.index] = (
                    densityMap[material.index] * (1.0 - fact))

        return fn.branching.map(fn_key=self.materialField, mapping=densityMap)

    def set_frictional_boundary(self, right=None, left=None,
                                top=None, bottom=None, front=None,
                                back=None, thickness=2):
        """ Set Frictional Boundary conditions

        Frictional boundaries are implemented as a thin layer of frictional
        material along the chosen boundaries.

        Parameters:
        -----------
        Returns:
        --------
            Underworld mesh variable that maps the boundaries.
            (Values are set to 1 if the mesh element applies
            a frictional condition, 0 otherwise).
        """

        self.frictionalBCs = FrictionBoundaries(self, rightFriction=right,
                                                leftFriction=left,
                                                topFriction=top,
                                                bottomFriction=bottom,
                                                frontFriction=front,
                                                backFriction=back,
                                                thickness=thickness)

        return self.frictionalBCs

    @property
    def _viscosityFn(self):
        """ Viscosity Function Builder"""

        ViscosityMap = {}
        BGViscosityMap = {}

        # Viscous behavior
        for material in self.materials:
            if material.viscosity:
                ViscosityHandler = material.viscosity
                ViscosityHandler.pressureField = self.pressureField
                ViscosityHandler.strainRateInvariantField = (
                    self.strainRate_2ndInvariant)
                ViscosityHandler.temperatureField = self.temperature
                ViscosityMap[material.index] = ViscosityHandler.muEff

        # Elasticity
        for material in self.materials:
            if material.elasticity:

                # We are dealing with viscous material
                # Raise an error is no viscosity has been defined.
                if not material.viscosity:
                    raise ValueError("""Viscosity undefined for
                                     {0}""".format(material.name))
                ElasticityHandler = material.elasticity
                ElasticityHandler.viscosity = ViscosityMap[material.index]
                ViscosityMap[material.index] = ElasticityHandler.muEff

        # Melt Modifier
        for material in self.materials:
            if material.melt:
                X1 = material.viscosityChangeX1
                X2 = material.viscosityChangeX2
                change = (1.0 + (material.viscosityChange - 1.0)
                          / (X2 - X1) * (self.meltField - X1))
                conditions = [(self.meltField < X1, 1.0),
                              (self.meltField > X2, material.viscosityChange),
                              (True, change)]
                ViscosityMap[material.index] *= fn.branching.conditional(
                    conditions)


        # Plasticity
        PlasticityMap = {}
        for material in self.materials:
            if material.plasticity:

                YieldHandler = material.plasticity
                YieldHandler.pressureField = self.pressureField
                YieldHandler.plasticStrain = self.plasticStrain

                if self.mesh.dim == 2:
                    yieldStress = YieldHandler._get_yieldStress2D()

                if self.mesh.dim == 3:
                    yieldStress = YieldHandler._get_yieldStress3D()

                if material.stressLimiter:
                    stressLimiter = StressLimiter(material.stressLimiter)
                    yieldStress = stressLimiter.apply(yieldStress)
                elif self.stressLimiter:
                    stressLimiter = StressLimiter(self.stressLimiter)
                    yieldStress = stressLimiter.apply(yieldStress)

                if material.elasticity:
                    ElasticityHandler = material.elasticity
                    mu = nd(ElasticityHandler.shear_modulus)
                    dt_e = nd(ElasticityHandler.observation_time)
                    strainRate = fn.tensor.symmetric(
                        self.velocityField.fn_gradient)
                    D_eff = (strainRate
                             + 0.5
                             * self._previousStressField
                             / (mu * dt_e))
                    SRInv = fn.tensor.second_invariant(D_eff)
                else:
                    SRInv = self.strainRate_2ndInvariant

                eij = fn.branching.conditional(
                    [(SRInv < nd(1e-20 / u.second), nd(1e-20 / u.second)),
                     (True, SRInv)])

                muEff = 0.5 * yieldStress / eij
                PlasticityMap[material.index] = muEff

            if self.frictionalBCs is not None:
                from copy import copy

                # Only affect plastic materials
                if material.plasticity:

                    YieldHandler = copy(material.plasticity)
                    YieldHandler.frictionCoefficient = self.frictionalBCs.friction
                    YieldHandler.frictionAfterSoftening = self.frictionalBCs.friction
                    YieldHandler.pressureField = self.pressureField
                    YieldHandler.plasticStrain = self.plasticStrain

                    if self.mesh.dim == 2:
                        yieldStress = YieldHandler._get_yieldStress2D()

                    if self.mesh.dim == 3:
                        yieldStress = YieldHandler._get_yieldStress3D()

                    eij = fn.branching.conditional(
                        [(self.strainRate_2ndInvariant <= 1e-20, 1e-20),
                         (True, self.strainRate_2ndInvariant)])

                    muEff = 0.5 * yieldStress / eij

                    conditions = [(self.frictionalBCs._mask > 0.0, muEff),
                                  (True, PlasticityMap[material.index])]

                    PlasticityMap[material.index] = (
                        fn.branching.conditional(conditions)
                    )

        # Combine rheologies
        EffViscosityMap = {}
        PlasticMap = {}
        for material in self.materials:
            idx = material.index
            if material.viscosity and material.plasticity:
                EffViscosityMap[idx] = fn.misc.min(PlasticityMap[idx],
                                                   ViscosityMap[idx])
                BGViscosityMap[idx] = ViscosityMap[idx]
                PlasticMap[idx] = 0.
            elif material.viscosity:
                EffViscosityMap[idx] = ViscosityMap[idx]
                BGViscosityMap[idx] = ViscosityMap[idx]
                PlasticMap[idx] = 0.
            elif material.plasticity:
                EffViscosityMap[idx] = PlasticityMap[idx]
                BGViscosityMap[idx] = PlasticityMap[idx]
                PlasticMap[idx] = 1.0

        # Apply viscosity Limiter
        for material in self.materials:
            idx = material.index
            if not material.minViscosity:
                minViscosity = self.minViscosity
            else:
                minViscosity = material.minViscosity

            if not material.maxViscosity:
                maxViscosity = self.maxViscosity
            else:
                maxViscosity = material.maxViscosity

            viscosity_limiter = ViscosityLimiter(minViscosity,
                                                 maxViscosity)

            if material.viscosity or material.plasticity:
                EffViscosityMap[idx] = (
                    viscosity_limiter.apply(EffViscosityMap[idx])
                )

        viscosityFn = fn.branching.map(fn_key=self.materialField,
                                       mapping=EffViscosityMap)

        backgroundViscosityFn = fn.branching.map(fn_key=self.materialField,
                                                 mapping=BGViscosityMap)

        isPlastic = fn.branching.map(fn_key=self.materialField,
                                     mapping=PlasticMap)
        yieldConditions = [(viscosityFn < backgroundViscosityFn, 1.0),
                           (isPlastic > 0.5, 1.0),
                           (True, 0.0)]

        # Do not yield at the very first solve
        if self._solution_exist.value:
            self._isYielding = (fn.branching.conditional(yieldConditions)
                                * self.strainRate_2ndInvariant)
        else:
            self._isYielding = fn.misc.constant(0.0)

        return viscosityFn

    @property
    def _stressFn(self):
        """Stress Function Builder"""
        stressMap = {}
        for material in self.materials:
            # Calculate viscous stress
            viscousStressFn = self._viscous_stressFn()
            elasticStressFn = self._elastic_stressFn
            stressMap[material.index] = viscousStressFn + elasticStressFn

        return fn.branching.map(fn_key=self.materialField,
                                mapping=stressMap)

    def _viscous_stressFn(self):
        """Viscous Stress Function Builder"""
        return 2. * self._viscosityFn * self.strainRate

    @property
    def _elastic_stressFn(self):
        """ Elastic Stress Function Builder"""
        if any([material.elasticity for material in self.materials]):
            stressMap = {}
            for material in self.materials:
                if material.elasticity:
                    ElasticityHandler = material.elasticity
                    ElasticityHandler.viscosity = self._viscosityFn
                    ElasticityHandler.previousStress = self._previousStressField
                    elasticStressFn = ElasticityHandler.elastic_stress
                else:
                    # Set elastic stress to zero if material
                    # has no elasticity
                    elasticStressFn = [0.0] * 3 if self.mesh.dim == 2 else [0.0] * 6
                stressMap[material.index] = elasticStressFn
            return (fn.branching.map(fn_key=self.materialField,
                                     mapping=stressMap))
        else:

            elasticStressFn = [0.0] * 3 if self.mesh.dim == 2 else [0.0] * 6
            return elasticStressFn

    def _update_stress_history(self, dt):
        """Update Previous Stress Field"""
        dt_e = []
        for material in self.materials:
            if material.elasticity:
                dt_e.append(nd(material.elasticity.observation_time))
        dt_e = np.array(dt_e).min()
        phi = dt / dt_e
        veStressFn_data = self._stressFn.evaluate(self.swarm)
        self._previousStressField.data[:] *= (1. - phi)
        self._previousStressField.data[:] += phi * veStressFn_data[:]

    @property
    def _yieldStressFn(self):
        """ Calculate Yield stress function from viscosity and strain rate"""
        eij = self.strainRate_2ndInvariant
        eijdef = nd(self.defaultStrainRate)
        return 2.0 * self._viscosityFn * fn.misc.max(eij, eijdef)

    def _phaseChangeFn(self):
        for material in self.materials:
            if material.phase_changes:
                for change in material.phase_changes:
                    obj = change
                    mask = obj.fn().evaluate(self.swarm)
                    conds = ((mask == 1) &
                             (self.materialField.data == material.index))
                    self.materialField.data[conds] = obj.result

    def solve_temperature_steady_state(self):
        """ Solve for steady state temperature

        Returns:
        --------
            Updated temperature Field
        """

        if self.materials:

            DiffusivityMap = {}
            for material in self.materials:
                if material.diffusivity:
                    DiffusivityMap[material.index] = nd(material.diffusivity)

            self.DiffusivityFn = fn.branching.map(
                fn_key=self.materialField,
                mapping=DiffusivityMap,
                fn_default=nd(self.diffusivity)
            )

            HeatProdMap = {}
            for material in self.materials:

                if all([material.density,
                        material.capacity,
                        material.radiogenicHeatProd]):

                    HeatProdMap[material.index] = (
                        nd(material.radiogenicHeatProd) / 
                        self._densityFn  / 
                        nd(material.capacity)
                    )

                else:
                    HeatProdMap[material.index] = 0.

                # Melt heating
                if material.latentHeatFusion and self._dt:
                    dynamicHeating = self._get_dynamic_heating(material)
                    HeatProdMap[material.index] += dynamicHeating

            self.HeatProdFn = fn.branching.map(fn_key=self.materialField,
                                               mapping=HeatProdMap)
        else:
            self.DiffusivityFn = fn.misc.constant(nd(self.diffusivity))
            self.HeatProdFn = fn.misc.constant(nd(self.radiogenicHeatProd))

        conditions = self._temperatureBCs

        heatequation = uw.systems.SteadyStateHeat(
            temperatureField=self.temperature,
            fn_diffusivity=self.DiffusivityFn,
            fn_heating=self.HeatProdFn,
            conditions=conditions
        )

        heatsolver = uw.systems.Solver(heatequation)
        heatsolver.solve(nonLinearIterate=True)

        return self.temperature

    def get_lithostatic_pressureField(self):
        """ Calculate the lithostatic Pressure Field

        Returns:
        --------
            Pressure Field, array containing the pressures
            at the bottom of the Model
        """

        gravity = np.abs(nd(self.gravity[-1]))
        lithoPress = LithostaticPressure(self.mesh, self._densityFn, gravity)
        self.pressureField.data[:], LPressBot = lithoPress.solve()
        self.pressSmoother.smooth()

        return self.pressureField, LPressBot

    def _calibrate_pressureField(self):
        """ Pressure Calibration callback function """

        surfaceArea = uw.utils.Integral(fn=1.0, mesh=self.mesh,
                                        integrationType='surface',
                                        surfaceIndexSet=self.top_wall)

        surfacepressureFieldIntegral = uw.utils.Integral(
            fn=self.pressureField,
            mesh=self.mesh,
            integrationType='surface',
            surfaceIndexSet=self.top_wall
        )
        area, = surfaceArea.evaluate()
        p0, = surfacepressureFieldIntegral.evaluate()
        offset = p0 / area
        self.pressureField.data[:] -= offset

    def _get_material_indices(self, material):
        """ Get mesh indices of a Material

        Parameter:
        ----------
            material:
                    The Material of interest
        Returns;
        --------
            underworld IndexSet

        """
        mat = self.projMaterialField.evaluate(self.mesh.data[0:self.mesh.nodesLocal])
        # Round to closest integer
        mat = np.rint(mat)
        # convert array as integer
        mat = mat.astype("int")[:, 0]
        # Get nodes corresponding to material
        nodes = np.arange(0, self.mesh.nodesLocal)[mat == material.index]
        return uw.mesh.FeMesh_IndexSet(self.mesh, topologicalIndex=0,
                                       size=self.mesh.nodesGlobal,
                                       fromObject=nodes)

    def solve(self):
        """ Solve Stokes """

        if self.step == 0:
            self._curTolerance = rcParams["initial.nonlinear.tolerance"]
            minIterations = rcParams["initial.nonlinear.min.iterations"]
            maxIterations = rcParams["initial.nonlinear.max.iterations"]
        else:
            self._curTolerance = rcParams["nonlinear.tolerance"]
            minIterations = rcParams["nonlinear.min.iterations"]
            maxIterations = rcParams["nonlinear.max.iterations"]

        self.get_stokes_solver().solve(
            nonLinearIterate=True,
            nonLinearMinIterations=minIterations,
            nonLinearMaxIterations=maxIterations,
            callback_post_solve=self.callback_post_solve,
            nonLinearTolerance=self._curTolerance)

        self._solution_exist.value = True

    def init_model(self, temperature=True, pressureField=True):
        """ Initialize the Temperature Field as steady state,
            Initialize the Pressure Field as Lithostatic

        Parameters:
        -----------
            temperature: (bool) default to True
            pressure: (bool) default to True
        """

        # Init Temperature Field
        if self.temperature and temperature:
            self.solve_temperature_steady_state()

        # Init pressureField Field
        if self.pressureField and pressureField:
            self.get_lithostatic_pressureField()
        return

    @u.check([None, "[time]", "[time]", None, None, None, "[time]", None, None])
    def run_for(self, duration=None, checkpoint_interval=None, nstep=None,
                checkpoint_times=None, restart_checkpoint=1, dt=None,
                restartStep=-1, restartDir=None):
        """ Run the Model

        Parameters
        ----------

        duration :
            Model time in units of time.
        checkpoint_interval :
            Checkpoint interval time.
        nstep :
            Number of steps to run.`
        checkpoint_times :
            Specify a list of additional Checkpoint times ([Time])
        restart_checkpoint :
            This parameter specify how often the swarm and swarm variables
            are checkpointed. A value of 1 means that the swarm and its
            associated variables are saved at every checkpoint.
            A value of 2 results in saving only every second checkpoint.
        dt :
            Specify the time interval (dt) to be used in
            units of time.
        restartStep :
            Restart Model. int (step number)
        restartDir :
            Restart Directory.

        """

        if uw.rank() == 0:
            print("""Running with UWGeodynamics version {0}""".format(full_version))
            sys.stdout.flush()

        if restartStep:
            restartDir = restartDir if restartDir else self.outputDir
            if os.path.exists(restartDir):
                self.restart(step=restartStep, restartDir=restartDir)
            uw.barrier()

        if ((checkpoint_interval or checkpoint_times) and
            uw.rank() == 0 and not os.path.exists(self.outputDir)):
            os.makedirs(self.outputDir)
        uw.barrier()

        stepDone = 0
        time = nd(self.time)

        if duration:
            units = duration.units
            duration = time + nd(duration)
        else:
            units = rcParams["time.SIunits"]
            duration = time

        if not nstep:
            nstep = stepDone

        if dt:
            user_dt = nd(dt)
        else:
            user_dt = None

        next_checkpoint = None

        if checkpoint_times:
            checkpoint_times = [nd(val) for val in checkpoint_times]

        if (isinstance(checkpoint_interval, u.Quantity) and
           checkpoint_interval.dimensionality == _dim_time):
            next_checkpoint = time + nd(checkpoint_interval)
        elif checkpoint_interval:
            next_checkpoint = stepDone + checkpoint_interval

        # Save initial state
        if checkpoint_interval or checkpoint_times:
            self.checkpoint()

        while time < duration or stepDone < nstep:

            self.preSolveHook()

            self.solve()

            # Whats the longest we can run before reaching the end
            # of the model or a checkpoint?
            # Need to generalize that
            self._dt = 2.0 * rcParams["CFL"] * self.swarm_advector.get_max_dt()

            if self.temperature:
                # Only get a condition if using SUPG
                if rcParams["advection.diffusion.method"] == "SUPG":
                    supg_dt = self._advdiffSystem.get_max_dt()
                    supg_dt *= 2.0 * rcParams["CFL"]
                    self._dt = min(self._dt, supg_dt)

            if (isinstance(checkpoint_interval, u.Quantity) and
               checkpoint_interval.dimensionality == _dim_time):
                self._dt = min(self._dt, next_checkpoint - time)

            if duration:
                self._dt = min(self._dt, duration - time)

            if user_dt:
                self._dt = min(self._dt, user_dt)

            if checkpoint_times:
                tcheck = [val for val in (checkpoint_times - time) if val >= 0]
                tcheck.sort()
                self._dt = min(self._dt, tcheck[0])

            dte = []
            for material in self.materials:
                if material.elasticity:
                    dte.append(nd(material.elasticity.observation_time))

            if dte:
                dte = np.array(dte).min()
                # Cap dt for observation time, dte / 3.
                if dte and self._dt > (dte / 3.):
                    self._dt = dte / 3.

            uw.barrier()

            self._update()

            self.step += 1
            stepDone += 1
            self.time += Dimensionalize(self._dt, units)
            time += self._dt

            if ((isinstance(checkpoint_interval, u.Quantity) and
               checkpoint_interval.dimensionality == _dim_time and
               time == next_checkpoint) or stepDone == next_checkpoint):
                    self.checkpointID += 1
                    # Save Mesh Variables
                    self.checkpoint_fields(checkpointID=self.checkpointID)
                    # Save Tracers
                    self.checkpoint_tracers(checkpointID=self.checkpointID)
                    next_checkpoint += nd(checkpoint_interval)

            uw.barrier()

            # if it's time to checkpoint the swarm, do so.
            if self.checkpointID % restart_checkpoint == 0:
                self.checkpoint_swarms(checkpointID=self.checkpointID)

            uw.barrier()

            if checkpoint_interval or self.step % 1 == 0 or nstep:
                if uw.rank() == 0:
                    print("Step:" + str(stepDone) + " Model Time: ", str(self.time.to(units)),
                          'dt:', str(Dimensionalize(self._dt, units)),
                          '(' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ')')
                    sys.stdout.flush()

            self.postSolveHook()

        return 1

    @staticmethod
    def preSolveHook():
        """ Entry point for functions to be run before attempting a solve """
        pass

    @staticmethod
    def postSolveHook():
        """ Entry point for functions to be run after the solve """
        pass

    @property
    def callback_post_solve(self):
        """ Function called right after a non-linear iteration or a linear
        solve"""
        return self._callback_post_solve

    @callback_post_solve.setter
    def callback_post_solve(self, value):
        def callback():
            if callable(value):
                value()
            if rcParams["surface.pressure.normalization"]:
                self._calibrate_pressureField()
            if rcParams["pressure.smoothing"]:
                self.pressSmoother.smooth()
            if self._isostasy:
                self._isostasy.solve()
            for material in self.materials:
                if material.viscosity:
                    material.viscosity.firstIter.value = False

            self._solution_exist.value = True
        self._callback_post_solve = callback

    def _update(self):
        """ Update Function

        The following function processes the mesh and swarm variables
        between two solves. It takes care of mesh, swarm advection and
        update the fields according to the Model state.

        """

        dt = self._dt

        # Heal plastic strain
        if any([material.healingRate for material in self.materials]):
            healingRates = {}
            for material in self.materials:
                healingRates[material.index] = nd(material.healingRate)
            HealingRateFn = fn.branching.map(fn_key=self.materialField,
                                             mapping=healingRates)

            plasticStrainIncHealing = dt * HealingRateFn.evaluate(self.swarm)
            self.plasticStrain.data[:] -= plasticStrainIncHealing
            self.plasticStrain.data[self.plasticStrain.data < 0.] = 0.

        # Increment plastic strain
        plasticStrainIncrement = dt * self._isYielding.evaluate(self.swarm)
        self.plasticStrain.data[:] += plasticStrainIncrement

        if any([material.melt for material in self.materials]):
            # Calculate New meltField
            self.update_melt_fraction()

        # Solve for temperature
        if self.temperature:
            self._advdiffSystem.integrate(dt)

        if self._advector:
            self.swarm_advector.integrate(dt)
            self._advector.advect_mesh(dt)
        elif self._freeSurface:
            self.swarm_advector.integrate(dt, update_owners=False)
            self._freeSurface.solve(dt)
            self.swarm.update_particle_owners()
        else:
            # Integrate Swarms in time
            self.swarm_advector.integrate(dt, update_owners=True)

        # Update stress
        if any([material.elasticity for material in self.materials]):
            self._update_stress_history(dt)

        if self.passive_tracers:
            for key in self.passive_tracers:
                self.passive_tracers[key].integrate(dt)

        # Do pop control
        self.population_control.repopulate()
        self.swarm.update_particle_owners()

        if self.surfaceProcesses:
            self.surfaceProcesses.solve(dt)

        # Update Time Field
        self.timeField.data[...] += dt


        if self._visugrid:
            self._visugrid.advect(dt)

        self._phaseChangeFn()

    def mesh_advector(self, axis):
        """ Initialize the mesh advector

        Parameters:
        -----------
            axis:
                list of axis (or degree of freedom) along which the
                mesh is allowed to deform
        """
        self._advector = _mesh_advector(self, axis)

    def add_passive_tracers(self, name, vertices=None,
                            particleEscape=True, centroids=None):
        """ Add a swarm of passive tracers to the Model

        Parameters:
        -----------
            name :
                Name of the swarm of tracers.
            vertices :
                Numpy array that contains the coordinates of the tracers.
            particleEscape : (bool)
                Allow or prevent tracers from escaping the boundaries of the
                Model (default to True)
            centroids : if a list of centroids is provided, the pattern defined
                by the vertices is reproduced around each centroid.
        """

        if centroids and not isinstance(centroids, list):
            centroids = list(centroids)

        if name in self.passive_tracers.keys():
            print("{0} tracers exists already".format(name))
            sys.stdout.flush()
            return self.passive_tracers[name]

        if not centroids:

            tracers = PassiveTracers(self.mesh,
                                     self.velocityField,
                                     name=name,
                                     vertices=vertices,
                                     particleEscape=particleEscape)

        else:

            tracers = PassiveTracersGrid(self.mesh,
                                         self.velocityField,
                                         name=name,
                                         vertices=vertices,
                                         centroids=centroids,
                                         particleEscape=particleEscape)

        self.passive_tracers[name] = tracers
        setattr(self, name.lower() + "_tracers", tracers)

        return tracers

    def _get_melt_fraction(self):
        """ Melt Fraction function

        Returns:
        -------
            Underworld function that calculates the Melt fraction on the
            particles.

        """
        meltMap = {}
        for material in self.materials:
            if material.melt:
                T_s = material.solidus.temperature(self.pressureField)
                T_l = material.liquidus.temperature(self.pressureField)
                T_ss = (self.temperature - 0.5 * (T_s + T_l)) / (T_l - T_s)
                value = (0.5 + T_ss + (T_ss * T_ss - 0.25) *
                         (0.4256 + 2.988 * T_ss))
                conditions = [((-0.5 < T_ss) & (T_ss < 0.5),
                               fn.misc.min(value, material.meltFractionLimit)),
                              (True, 0.0)]
                meltMap[material.index] = fn.branching.conditional(conditions)

        return fn.branching.map(fn_key=self.materialField,
                                mapping=meltMap, fn_default=0.0)

    def update_melt_fraction(self):
        """ Calculate New meltField """
        meltFraction = self._get_melt_fraction()
        self.meltField.data[:] = meltFraction.evaluate(self.swarm)

    def _get_dynamic_heating(self, material):
        """ Calculate additional heating source due to melt

        Returns:
        --------
            Underworld function

        """

        ratio = material.latentHeatFusion / material.capacity

        if not ratio.dimensionless:
            raise ValueError("""Unit Error in either Latent Heat Fusion or
                             Capacity (Material: """ + material.name)
        ratio = ratio.magnitude

        dF = (self._get_melt_fraction() - self.meltField) / self._dt
        return (ratio * dF) * self.temperature

    @property
    def _lambdaFn(self):
        """ Initialize compressibility """

        materialMap = {}
        if any([material.compressibility for material in self.materials]):
            for material in self.materials:
                if material.compressibility:
                    materialMap[material.index] = nd(material.compressibility)

            return (uw.function.branching.map(fn_key=self.materialField,
                                                  mapping=materialMap,
                                                  fn_default=0.0))
        return

    @property
    def freeSurface(self):
        return self._freeSurface

    @freeSurface.setter
    def freeSurface(self, value):
        if value:
            self._freeSurface = FreeSurfaceProcessor(self)

    def checkpoint(self, checkpointID=None, variables=None):
        """ Do a checkpoint (Save fields)

        Parameters:
        -----------
            variables:
                list of fields/variables to save
            checkpointID:
                checkpoint ID.

        """

        self.checkpoint_fields(variables, checkpointID)
        self.checkpoint_swarms(variables, checkpointID)
        self.checkpoint_tracers(checkpointID=checkpointID)

    def add_visugrid(self, elementRes, minCoord=None, maxCoord=None):
        """ Add a tracking grid to the Model

        This is essentially a lagrangian grid that deforms with the materials.

        Parameters:
        -----------
            elementRes:
                Grid resolution in number of elements
                along each axis (x, y, z).
            minCoord:
                Minimum coordinate for each axis.
                Used to define the extent of the grid,
            maxCoord:
                Maximum coordinate for each axis.
                Used to define the extent of the grid,
        """

        if not maxCoord:
            maxCoord = self.maxCoord

        if not minCoord:
            minCoord = self.minCoord

        self._visugrid = Visugrid(self, elementRes, minCoord, maxCoord,
                                  self.velocityField)

    def checkpoint_fields(self, fields=None, checkpointID=None, time=None,
                          outputDir=None):
        """ Save the mesh and the mesh variables to outputDir

        Parameters
        ----------

        fields : A list of mesh/field variables to be saved.
        checkpointID : Checkpoint ID
        time : Model time at checkpoint
        outputDir : output directory

        """

        if not fields:
            fields = rcParams["default.outputs"]

        if not checkpointID:
            checkpointID = self.checkpointID

        if not outputDir:
            outputDir = self.outputDir

        if uw.rank() == 0 and not os.path.exists(outputDir):
            os.makedirs(outputDir)
        uw.barrier()

        time = time if time else self.time

        if self._advector or self._freeSurface:
            mesh_name = 'mesh-%s' % checkpointID
            mesh_prefix = os.path.join(outputDir, mesh_name)
            mH = self.mesh.save('%s.h5' % mesh_prefix, units=u.kilometers,
                                time=time)
        elif not self._mesh_saved:
            mesh_name = 'mesh'
            mesh_prefix = os.path.join(outputDir, mesh_name)
            mH = self.mesh.save('%s.h5' % mesh_prefix, units=u.kilometers,
                                time=time)
            self._mesh_saved = True
        else:
            mesh_name = 'mesh'
            mesh_prefix = os.path.join(outputDir, mesh_name)
            mH = uw.utils.SavedFileData(self.mesh, '%s.h5' % mesh_prefix)

        filename = "XDMF.fields." + str(checkpointID).zfill(5) + ".xmf"
        filename = os.path.join(outputDir, filename)

        # First write the XDMF header
        string = uw.utils._xdmfheader()
        string += uw.utils._spacetimeschema(mH, mesh_name, time)

        for field in fields:
            if field == "temperature" and not self.temperature:
                continue
            if field in rcParams["mesh.variables"]:
                field = str(field)
                try:
                    units = rcParams[field + ".SIunits"]
                except KeyError:
                    units = None

                # Save the h5 file and write the field schema for
                # each one of the field variables
                obj = getattr(self, field)
                file_prefix = os.path.join(outputDir, field + '-%s' % checkpointID)
                handle = obj.save('%s.h5' % file_prefix, units=units,
                                  time=time)
                string += uw.utils._fieldschema(handle, field)

        # Write the footer to the xmf
        string += uw.utils._xdmffooter()

        # Write the string to file - only proc 0
        if uw.rank() == 0:
            with open(filename, "w") as xdmfFH:
                xdmfFH.write(string)
        uw.barrier()

    def checkpoint_swarms(self, fields=None, checkpointID=None, time=None,
                          outputDir=None):
        """ Save the swarm and the swarm variables to outputDir

        Parameters
        ----------

        fields : A list of swarm/field variables to be saved.
        checkpointID : Checkpoint ID
        time : Model time at checkpoint
        outputDir : output directory

        """

        if not fields:
            fields = rcParams["restart.fields"]

        if not checkpointID:
            checkpointID = self.checkpointID

        if not outputDir:
            outputDir = self.outputDir

        if uw.rank() == 0 and not os.path.exists(outputDir):
            os.makedirs(outputDir)
        uw.barrier()

        time = time if time else self.time
        swarm_name = 'swarm-%s.h5' % checkpointID

        sH = self.swarm.save(os.path.join(outputDir,
                             swarm_name),
                             units=u.kilometers,
                             time=time)

        filename = "XDMF.swarms." + str(checkpointID).zfill(5) + ".xmf"
        filename = os.path.join(outputDir, filename)

        # First write the XDMF header
        string = uw.utils._xdmfheader()
        string += uw.utils._swarmspacetimeschema(sH, swarm_name, time)

        for field in fields:
            if field in rcParams["swarm.variables"]:
                field = str(field)
                try:
                    units = rcParams[field + ".SIunits"]
                except KeyError:
                    units = None

                # Save the h5 file and write the field schema for
                # each one of the field variables
                obj = getattr(self, field)
                file_prefix = os.path.join(outputDir, field + '-%s' % checkpointID)
                handle = obj.save('%s.h5' % file_prefix, units=units, time=time)
                string += uw.utils._swarmvarschema(handle, field)

        # Write the footer to the xmf
        string += uw.utils._xdmffooter()

        # Write the string to file - only proc 0
        if uw.rank() == 0:
            with open(filename, "w") as xdmfFH:
                xdmfFH.write(string)
        uw.barrier()

    @u.check([None, None, None, "[time]", None])
    def checkpoint_tracers(self, tracers=None, checkpointID=None,
                           time=None, outputDir=None):
        """ Checkpoint the tracers

        Parameters
        ----------

        tracers : List of tracers to checkpoint.
        checkpointID : Checkpoint ID.
        time : Model time at checkpoint.
        outputDir : output directory

        """

        if not checkpointID:
            checkpointID = self.checkpointID

        time = time if time else self.time

        if not outputDir:
            outputDir = self.outputDir

        if uw.rank() == 0 and not os.path.exists(outputDir):
            os.makedirs(outputDir)
        uw.barrier()

        # Checkpoint passive tracers and associated tracked fields
        if self.passive_tracers:
            for (dump, item) in self.passive_tracers.items():
                item.save(outputDir, checkpointID, time)

    def geometry_from_shapefile(self, filename, units=None):
        from ._utils import MoveImporter
        Importer = MoveImporter(filename, units=units)

        shape_dict = {name: [] for name in Importer.names}

        for polygon in Importer.generator:
            name = polygon["properties"]["Name"]
            vertices = polygon["coordinates"]
            shape = shapes.Polygon(vertices)
            shape_dict[name].append(shape)

        for name, shape in shape_dict.items():
            mshape = shapes.MultiShape(shape)
            self.add_material(name=name,
                              shape=mshape,
                              fill=False)

        self._fill_model()

    def velocity_rms(self):
        vdotv_fn = uw.function.math.dot(self.velocityField, self.velocityField)
        fn_2_integrate = (1., vdotv_fn)
        (v2, vol) = self.mesh.integrate(fn=fn_2_integrate)
        import math
        vrms = math.sqrt(v2 / vol)
        #os.write(1, "Velocity rms (vrms): {0}".format(vrms))
        return vrms

_html_global = OrderedDict()
_html_global["Number of Elements"] = "elementRes"
_html_global["length"] = "length"
_html_global["width"] = "width"
_html_global["height"] = "height"


def _model_html_repr(Model):
    header = "<table>"
    footer = "</table>"
    html = ""
    for key, val in _html_global.items():
        value = Model.__dict__.get(val)
        html += "<tr><td>{0}</td><td>{1}</td></tr>".format(key, value)

    return header + html + footer
