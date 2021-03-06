import numpy as np
import os
from ctapipe.core.traits import Unicode, List, Int, Bool
from ctapipe.calib.camera import CameraCalibrator
from ctapipe.image.reducer import DataVolumeReducer
from ctapipe.image.extractor import ImageExtractor
from ctapipe.io.hdf5tableio import HDF5TableReader
from ctapipe.containers import MonitoringContainer
from ctapipe.calib.camera import gainselection
from lstchain.calib.camera.pulse_time_correction import PulseTimeCorrection


__all__ = ['LSTCameraCalibrator']


class LSTCameraCalibrator(CameraCalibrator):
    """
    Calibrator to handle the LST camera calibration chain, in order to fill
    the DL1 data level in the event container.
    """
    extractor_product = Unicode(
        'LocalPeakWindowSum',
        help='Name of the charge extractor to be used'
    ).tag(config=True)

    reducer_product = Unicode(
        'NullDataVolumeReducer',
        help='Name of the DataVolumeReducer to use'
    ).tag(config=True)

    calibration_path = Unicode(
        '',
        help='Path to LST calibration file'
    ).tag(config=True)

    time_calibration_path = Unicode(
        '',
        help='Path to drs4 time calibration file'
    ).tag(config=True)

    allowed_tels = List(
        [1],
        help='List of telescope to be calibrated'
    ).tag(config=True)

    gain_threshold = Int(
        4094,
        allow_none=True,
        help='Threshold for the gain selection in ADC'
    ).tag(config=True)

    charge_scale = List(
        [1,1],
        help='Multiplicative correction factor for charge estimation [HG,LG]'
    ).tag(config=True)


    def __init__(self, subarray, **kwargs):
        """
        Parameters
        ----------

        reducer_product : ctapipe.image.reducer.DataVolumeReducer
            The DataVolumeReducer to use. If None, then
            NullDataVolumeReducer will be used by default, and waveforms
            will not be reduced.
        extractor_product : ctapipe.image.extractor.ImageExtractor
            The ImageExtractor to use. If None, then LocalPeakWindowSum
            will be used by default.
        calibration_path :
            Path to LST calibration file to get the pedestal and flat-field corrections


        kwargs
        """
        super().__init__(subarray, **kwargs)

        # load the waveform charge extractor
        self.image_extractor = ImageExtractor.from_name(
            self.extractor_product,
            subarray = self.subarray,
            config = self.config
        )
        self.log.info(f"extractor {self.extractor_product}")

        print("EXTRACTOR", self.image_extractor)

        self.data_volume_reducer = DataVolumeReducer.from_name(
            self.reducer_product,
            subarray=self.subarray,
            config = self.config
        )
        self.log.info(f" {self.reducer_product}")


        # declare gain selector if the threshold is defined
        if self.gain_threshold:
            self.gain_selector = gainselection.ThresholdGainSelector(
                threshold=self.gain_threshold
            )

        # declare time calibrator if correction file exist
        if os.path.exists(self.time_calibration_path):
            self.time_corrector = PulseTimeCorrection(
                calib_file_path=self.time_calibration_path
            )
        else:
            raise IOError(f"Time calibration file {self.time_calibration_path} not found!")

        # calibration data container
        self.mon_data = MonitoringContainer()

        # initialize the MonitoringContainer() for the moment it reads it from a hdf5 file
        self._initialize_correction()


        self.log.info(f"Global charge scale {self.charge_scale}")


    def _initialize_correction(self):
        """
        Read the correction from hdf5 calibration file
        """

        self.mon_data.tels_with_data = self.allowed_tels
        self.log.info(f"read {self.calibration_path}")

        try:
            with HDF5TableReader(self.calibration_path) as h5_table:
                for telid in self.allowed_tels:
                    # read the calibration data
                    table = '/tel_' + str(telid) + '/calibration'
                    next(h5_table.read(table, self.mon_data.tel[telid].calibration))

                    # read pedestal data
                    table = '/tel_' + str(telid) + '/pedestal'
                    next(h5_table.read(table, self.mon_data.tel[telid].pedestal))

                    # read flat-field data
                    table = '/tel_' + str(telid) + '/flatfield'
                    next(h5_table.read(table, self.mon_data.tel[telid].flatfield))

                    # read the pixel_status container
                    table = '/tel_' + str(telid) + '/pixel_status'
                    next(h5_table.read(table, self.mon_data.tel[telid].pixel_status))
        except Exception:
            self.log.exception(
                f"Problem in reading calibration file {self.calibration_path}"
            )
            raise


    def _calibrate_dl0(self, event, telid):
        """
        create dl0 level, for the moment copy the r1
        """
        waveforms = event.r1.tel[telid].waveform

        if self._check_r1_empty(waveforms):
            return

        # if not already done, initialize the event monitoring containers
        if event.mon.tel[telid].calibration.dc_to_pe is None:
            event.mon.tel[telid].calibration = self.mon_data.tel[telid].calibration
            event.mon.tel[telid].flatfield = self.mon_data.tel[telid].flatfield
            event.mon.tel[telid].pedestal = self.mon_data.tel[telid].pedestal
            event.mon.tel[telid].pixel_status = self.mon_data.tel[telid].pixel_status

        #
        # subtract the pedestal per sample and multiply for the calibration coefficients
        #
        event.dl0.tel[telid].waveform = (
                (waveforms - self.mon_data.tel[telid].calibration.pedestal_per_sample[:, :, np.newaxis])
                * self.mon_data.tel[telid].calibration.dc_to_pe[:, :, np.newaxis]).astype(np.float32)


    def _calibrate_dl1(self, event, telid):
        """
        create calibrated dl1 image and calibrate it
        """
        waveforms = event.dl0.tel[telid].waveform

        if self._check_dl0_empty(waveforms):
            return

        # for the moment we do the gain selection afterwards
        # use gain mask without gain selection

        # TBD: - perform calibration of the R1 waveform (not DL1)
        #      - gain selection before charge integration

        # In case of no gain selection the selected gain channels are  [0,0,..][1,1,..]
        no_gain_selection = np.zeros((waveforms.shape[0], waveforms.shape[1]), dtype=np.int)
        no_gain_selection[1] = 1

        charge = np.zeros((waveforms.shape[0], waveforms.shape[1]),
                          dtype='float32')
        peak_time = np.zeros((waveforms.shape[0], waveforms.shape[1]),
                             dtype='float32')
        # image extraction for each channel:
        for i in range(waveforms.shape[0]):
            charge[i], peak_time[i] = self.image_extractor(waveforms[i], telid, no_gain_selection[i])


        # correct charge for global scale
        corrected_charge = charge * np.array(self.charge_scale, dtype=np.float32)[:, np.newaxis]

        # correct time with drs4 correction if available
        if self.time_corrector:
            peak_time = self.time_corrector.get_corr_pulse(event, peak_time)

        # add flat-fielding time correction
        peak_time_ff_corrected = peak_time + self.mon_data.tel[telid].calibration.time_correction.value

        # perform the gain selection if the threshold is defined
        if self.gain_threshold:
            gain_mask = self.gain_selector(event.r1.tel[telid].waveform)

            event.dl1.tel[telid].image = corrected_charge[gain_mask, np.arange(charge.shape[1])]
            event.dl1.tel[telid].peak_time = \
                peak_time_ff_corrected[gain_mask, np.arange(peak_time_ff_corrected.shape[1])].astype(np.float32)

            # remember which channel has been selected
            event.r1.tel[telid].selected_gain_channel = gain_mask

        # if threshold == None
        else:
            event.dl1.tel[telid].image = corrected_charge
            event.dl1.tel[telid].peak_time = peak_time_ff_corrected

