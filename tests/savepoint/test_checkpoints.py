import dataclasses
import os
from datetime import timedelta
from typing import List, Tuple

import dacite
import f90nml
import xarray as xr
import yaml

from ndsl import (
    CompilationConfig,
    CubedSphereCommunicator,
    CubedSpherePartitioner,
    GridIndexing,
    MPIComm,
    Namelist,
    Quantity,
    QuantityFactory,
    StencilConfig,
    StencilFactory,
    SubtileGridSizer,
    TilePartitioner,
)
from ndsl.checkpointer import (
    SavepointThresholds,
    Threshold,
    ThresholdCalibrationCheckpointer,
    ValidationCheckpointer,
)
from ndsl.grid import DampingCoefficients, GridData
from ndsl.stencils.testing import Grid, TranslateGrid, dataset_to_dict
from ndsl.testing import perturb
from pyFV3 import DycoreState, DynamicalCore, DynamicalCoreConfig
from pyFV3.testing import TranslateFVDynamics


def get_grid(data_path: str, rank: int, layout: Tuple[int, int], backend: str) -> Grid:
    ds_grid: xr.Dataset = xr.open_dataset(os.path.join(data_path, "Grid-Info.nc")).isel(
        savepoint=0
    )
    grid = TranslateGrid(
        dataset_to_dict(ds_grid.isel(rank=rank)),
        rank=rank,
        layout=layout,
        backend=backend,
    ).python_grid()
    return grid


class StateInitializer:
    def __init__(
        self,
        ds: xr.Dataset,
        translate: TranslateFVDynamics,
    ):
        self._ds = ds
        self._translate = translate

    def new_state(self) -> Tuple[DycoreState, GridData]:
        input_data = dataset_to_dict(self._ds.copy())
        state, grid_data = self._translate.prepare_data(input_data)
        return state, grid_data


def test_fv_dynamics(
    backend: str, data_path: str, calibrate_thresholds: bool, threshold_path: str
):
    print("start test call")
    namelist = Namelist.from_f90nml(f90nml.read(os.path.join(data_path, "input.nml")))
    threshold_filename = os.path.join(threshold_path, "fv_dynamics.yaml")
    communicator = CubedSphereCommunicator(
        comm=MPIComm(),
        partitioner=CubedSpherePartitioner(
            tile=TilePartitioner(layout=namelist.layout)
        ),
    )
    stencil_factory = StencilFactory(
        config=StencilConfig(
            compilation_config=CompilationConfig(
                backend=backend,
                communicator=communicator,
                rebuild=False,
            )
        ),
        grid_indexing=GridIndexing.from_sizer_and_communicator(
            sizer=SubtileGridSizer.from_tile_params(
                nx_tile=namelist.npx - 1,
                ny_tile=namelist.npy - 1,
                nz=namelist.npz,
                n_halo=3,
                tile_partitioner=communicator.partitioner.tile,
                tile_rank=communicator.rank,
                extra_dim_lengths={},
                layout=namelist.layout,
            ),
            comm=communicator,
        ),
    )
    grid = get_grid(
        data_path=data_path,
        rank=communicator.rank,
        layout=namelist.layout,
        backend=backend,
    )
    translate = TranslateFVDynamics(
        grid=grid, namelist=namelist, stencil_factory=stencil_factory
    )
    ds = xr.open_dataset(os.path.join(data_path, "FVDynamics-In.nc")).sel(
        savepoint=0, rank=communicator.rank
    )
    dycore_config = DynamicalCoreConfig.from_namelist(namelist)
    initializer = StateInitializer(
        ds,
        translate,
    )
    if calibrate_thresholds:
        thresholds = _calibrate_thresholds(
            initializer=initializer,
            communicator=communicator,
            stencil_factory=stencil_factory,
            quantity_factory=grid.quantity_factory,
            damping_coefficients=grid.damping_coefficients,
            dycore_config=dycore_config,
            n_trials=10,
            factor=10.0,
        )
        print(f"calibrated thresholds: {thresholds}")
        if communicator.rank == 0:
            with open(threshold_filename, "w") as f:
                yaml.safe_dump(dataclasses.asdict(thresholds), f)
        communicator.comm.barrier()
    with open(threshold_filename, "r") as f:
        data = yaml.safe_load(f)
        thresholds = dacite.from_dict(
            data_class=SavepointThresholds,
            data=data,
            config=dacite.Config(strict=True),
        )
    validation = ValidationCheckpointer(
        savepoint_data_path=data_path, thresholds=thresholds, rank=communicator.rank
    )
    state, grid_data = initializer.new_state()
    dycore = DynamicalCore(
        comm=communicator,
        grid_data=grid_data,
        stencil_factory=stencil_factory,
        quantity_factory=grid.quantity_factory,
        damping_coefficients=grid.damping_coefficients,
        config=dycore_config,
        phis=state.phis,
        state=state,
        checkpointer=validation,
        timestep=timedelta(seconds=dycore_config.dt_atmos),
    )
    with validation.trial():
        dycore.step_dynamics(state)


def _calibrate_thresholds(
    initializer: StateInitializer,
    communicator: CubedSphereCommunicator,
    stencil_factory: StencilFactory,
    quantity_factory: QuantityFactory,
    damping_coefficients: DampingCoefficients,
    dycore_config: DynamicalCoreConfig,
    n_trials: int,
    factor: float,
):
    calibration = ThresholdCalibrationCheckpointer(factor=factor)
    for i in range(n_trials):
        print(f"running calibration trial {i}")
        trial_state, grid_data = initializer.new_state()
        perturb(dycore_state_to_dict(trial_state))
        # we need to initialize new DynamicalCore because halo updates bind
        # to a particular state object, currently
        dycore = DynamicalCore(
            comm=communicator,
            grid_data=grid_data,
            stencil_factory=stencil_factory,
            quantity_factory=quantity_factory,
            damping_coefficients=damping_coefficients,
            config=dycore_config,
            phis=trial_state.phis,
            state=trial_state,
            checkpointer=calibration,
            timestep=timedelta(seconds=dycore_config.dt_atmos),
        )
        with calibration.trial():
            dycore.step_dynamics(trial_state)
    all_thresholds = communicator.comm.allgather(calibration.thresholds)
    thresholds = merge_thresholds(all_thresholds)
    set_manual_thresholds(thresholds)
    return thresholds


def set_manual_thresholds(thresholds: SavepointThresholds):
    # all thresholds on the input data are 0 because no computation has happened yet
    for entry in thresholds.savepoints["FVDynamics-In"]:
        for name in entry:
            entry[name] = Threshold(relative=0.0, absolute=0.0)


def merge_thresholds(all_thresholds: List[SavepointThresholds]):
    thresholds = all_thresholds[0]
    for other_thresholds in all_thresholds[1:]:
        for savepoint_name in thresholds.savepoints:
            for i_call in range(len(thresholds.savepoints[savepoint_name])):
                for variable_name in thresholds.savepoints[savepoint_name][i_call]:
                    thresholds.savepoints[savepoint_name][i_call][
                        variable_name
                    ] = thresholds.savepoints[savepoint_name][i_call][
                        variable_name
                    ].merge(
                        other_thresholds.savepoints[savepoint_name][i_call][
                            variable_name
                        ]
                    )
    return thresholds


def dycore_state_to_dict(state: DycoreState):
    return {
        name: getattr(state, name).data
        for name in dir(state)
        if isinstance(getattr(state, name), Quantity)
    }
