# Support for eddy current based Z probes
#
# Copyright (C) 2021-2024  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, math, bisect
import mcu
from . import ldc1612, probe, manual_probe
from mathutil import Polynomial2d

# Tool for calibrating the sensor Z detection and applying that calibration
class EddyCalibration:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.drift_comp = EddyDriftCompensation(config)
        # Current calibration data
        self.cal_freqs = []
        self.cal_zpos = []
        self.cal_temp = config.getfloat('calibration_temp', 0)
        cal = config.get('calibrate', None)
        if cal is not None:
            cal = [list(map(float, d.strip().split(':', 1)))
                   for d in cal.split(',')]
            self.load_calibration(cal)
        # Probe calibrate state
        self.probe_speed = 0.
        # Register commands
        cname = self.name.split()[-1]
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("PROBE_EDDY_CURRENT_CALIBRATE", "CHIP",
                                   cname, self.cmd_EDDY_CALIBRATE,
                                   desc=self.cmd_EDDY_CALIBRATE_help)
    def is_calibrated(self):
        return len(self.cal_freqs) > 2
    def load_calibration(self, cal):
        cal = sorted([(c[1], c[0]) for c in cal])
        self.cal_freqs = [c[0] for c in cal]
        self.cal_zpos = [c[1] for c in cal]
    def apply_calibration(self, samples):
        for i, (samp_time, freq, dummy_z) in enumerate(samples):
            adj_freq = self.drift_comp.adjust_freq(freq, self.cal_temp)
            pos = bisect.bisect(self.cal_freqs, adj_freq)
            if pos >= len(self.cal_zpos):
                zpos = -99.9
            elif pos == 0:
                zpos = 99.9
            else:
                # XXX - could further optimize and avoid div by zero
                this_freq = self.cal_freqs[pos]
                prev_freq = self.cal_freqs[pos - 1]
                this_zpos = self.cal_zpos[pos]
                prev_zpos = self.cal_zpos[pos - 1]
                gain = (this_zpos - prev_zpos) / (this_freq - prev_freq)
                offset = prev_zpos - prev_freq * gain
                zpos = adj_freq * gain + offset
            samples[i] = (samp_time, freq, round(zpos, 6))
    def height_to_freq(self, height):
        # XXX - could optimize lookup
        rev_zpos = list(reversed(self.cal_zpos))
        rev_freqs = list(reversed(self.cal_freqs))
        pos = bisect.bisect(rev_zpos, height)
        if pos == 0 or pos >= len(rev_zpos):
            raise self.printer.command_error(
                "Invalid probe_eddy_current height")
        this_freq = rev_freqs[pos]
        prev_freq = rev_freqs[pos - 1]
        this_zpos = rev_zpos[pos]
        prev_zpos = rev_zpos[pos - 1]
        gain = (this_freq - prev_freq) / (this_zpos - prev_zpos)
        offset = prev_freq - prev_zpos * gain
        freq = height * gain + offset
        return self.drift_comp.lookup_freq(freq, self.cal_temp)
    def do_calibration_moves(self, move_speed):
        toolhead = self.printer.lookup_object('toolhead')
        kin = toolhead.get_kinematics()
        move = toolhead.manual_move
        # Start data collection
        msgs = []
        is_finished = False
        def handle_batch(msg):
            if is_finished:
                return False
            msgs.append(msg)
            return True
        self.printer.lookup_object(self.name).add_client(handle_batch)
        toolhead.dwell(1.)
        temp = self.drift_comp.get_temperature()
        # Move to each 50um position
        max_z = 4
        samp_dist = 0.050
        num_steps = int(max_z / samp_dist + .5) + 1
        start_pos = toolhead.get_position()
        times = []
        for i in range(num_steps):
            # Move to next position (always descending to reduce backlash)
            hop_pos = list(start_pos)
            hop_pos[2] += i * samp_dist + 0.500
            move(hop_pos, move_speed)
            next_pos = list(start_pos)
            next_pos[2] += i * samp_dist
            move(next_pos, move_speed)
            # Note sample timing
            start_query_time = toolhead.get_last_move_time() + 0.050
            end_query_time = start_query_time + 0.100
            toolhead.dwell(0.200)
            # Find Z position based on actual commanded stepper position
            toolhead.flush_step_generation()
            kin_spos = {s.get_name(): s.get_commanded_position()
                        for s in kin.get_steppers()}
            kin_pos = kin.calc_position(kin_spos)
            times.append((start_query_time, end_query_time, kin_pos[2]))
        toolhead.dwell(1.0)
        toolhead.wait_moves()
        temp = (temp + self.drift_comp.get_temperature()) / 2.
        # Finish data collection
        is_finished = True
        # Correlate query responses
        cal = {}
        step = 0
        for msg in msgs:
            for query_time, freq, old_z in msg['data']:
                # Add to step tracking
                while step < len(times) and query_time > times[step][1]:
                    step += 1
                if step < len(times) and query_time >= times[step][0]:
                    cal.setdefault(times[step][2], []).append(freq)
        if len(cal) != len(times):
            raise self.printer.command_error(
                "Failed calibration - incomplete sensor data")
        return cal, temp
    def calc_freqs(self, meas):
        total_count = total_variance = 0
        positions = {}
        for pos, freqs in meas.items():
            count = len(freqs)
            freq_avg = float(sum(freqs)) / count
            positions[pos] = freq_avg
            total_count += count
            total_variance += sum([(f - freq_avg)**2 for f in freqs])
        return positions, math.sqrt(total_variance / total_count), total_count
    def post_manual_probe(self, kin_pos):
        if kin_pos is None:
            # Manual Probe was aborted
            return
        curpos = list(kin_pos)
        move = self.printer.lookup_object('toolhead').manual_move
        # Move away from the bed
        probe_calibrate_z = curpos[2]
        curpos[2] += 5.
        move(curpos, self.probe_speed)
        # Move sensor over nozzle position
        pprobe = self.printer.lookup_object("probe")
        x_offset, y_offset, z_offset = pprobe.get_offsets()
        curpos[0] -= x_offset
        curpos[1] -= y_offset
        move(curpos, self.probe_speed)
        # Descend back to bed
        curpos[2] -= 5. - 0.050
        move(curpos, self.probe_speed)
        # Perform calibration movement and capture
        cal, temp = self.do_calibration_moves(self.probe_speed)
        # Calculate each sample position average and variance
        positions, std, total = self.calc_freqs(cal)
        last_freq = 0.
        for pos, freq in reversed(sorted(positions.items())):
            if freq <= last_freq:
                raise self.printer.command_error(
                    "Failed calibration - frequency not increasing each step")
            last_freq = freq
        gcode = self.printer.lookup_object("gcode")
        gcode.respond_info(
            "probe_eddy_current: stddev=%.3f in %d queries\n"
            "The SAVE_CONFIG command will update the printer config file\n"
            "and restart the printer." % (std, total))
        # Save results
        cal_contents = []
        for i, (pos, freq) in enumerate(sorted(positions.items())):
            if not i % 3:
                cal_contents.append('\n')
            cal_contents.append("%.6f:%.3f" % (pos - probe_calibrate_z, freq))
            cal_contents.append(',')
        cal_contents.pop()
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.name, 'calibrate', ''.join(cal_contents))
        configfile.set(self.name, 'calibration_temp', "%.6f" % (temp,))
    cmd_EDDY_CALIBRATE_help = "Calibrate eddy current probe"
    def cmd_EDDY_CALIBRATE(self, gcmd):
        self.probe_speed = gcmd.get_float("PROBE_SPEED", 5., above=0.)
        # Start manual probe
        manual_probe.ManualProbeHelper(self.printer, gcmd,
                                       self.post_manual_probe)

# Helper for implementing PROBE style commands
class EddyEndstopWrapper:
    HAS_SCANNING = True
    def __init__(self, config, sensor_helper, calibration):
        self._printer = config.get_printer()
        self._sensor_helper = sensor_helper
        self._mcu = sensor_helper.get_mcu()
        self._calibration = calibration
        self._z_offset = config.getfloat('z_offset', minval=0.)
        self._dispatch = mcu.TriggerDispatch(self._mcu)
        self._samples = []
        self._is_sampling = self._start_from_home = self._need_stop = False
        self._trigger_time = 0.
        self._printer.register_event_handler('klippy:mcu_identify',
                                             self._handle_mcu_identify)
    def _handle_mcu_identify(self):
        kin = self._printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis('z'):
                self.add_stepper(stepper)
    # Measurement gathering
    def _start_measurements(self, is_home=False):
        self._need_stop = False
        if self._is_sampling:
            return
        self._is_sampling = True
        self._is_from_home = is_home
        self._sensor_helper.add_client(self._add_measurement)
    def _stop_measurements(self, is_home=False):
        if not self._is_sampling or (is_home and not self._start_from_home):
            return
        self._need_stop = True
    def _add_measurement(self, msg):
        if self._need_stop:
            del self._samples[:]
            self._is_sampling = self._need_stop = False
            return False
        self._samples.append(msg)
        return True
    # Interface for MCU_endstop
    def get_mcu(self):
        return self._mcu
    def add_stepper(self, stepper):
        self._dispatch.add_stepper(stepper)
    def get_steppers(self):
        return self._dispatch.get_steppers()
    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        self._trigger_time = 0.
        self._start_measurements(is_home=True)
        trigger_freq = self._calibration.height_to_freq(self._z_offset)
        trigger_completion = self._dispatch.start(print_time)
        self._sensor_helper.setup_home(
            print_time, trigger_freq, self._dispatch.get_oid(),
            mcu.MCU_trsync.REASON_ENDSTOP_HIT)
        return trigger_completion
    def home_wait(self, home_end_time):
        self._dispatch.wait_end(home_end_time)
        trigger_time = self._sensor_helper.clear_home()
        self._stop_measurements(is_home=True)
        res = self._dispatch.stop()
        if res == mcu.MCU_trsync.REASON_COMMS_TIMEOUT:
            return -1.
        if res != mcu.MCU_trsync.REASON_ENDSTOP_HIT:
            return 0.
        if self._mcu.is_fileoutput():
            return home_end_time
        self._trigger_time = trigger_time
        return trigger_time
    def query_endstop(self, print_time):
        return False # XXX
    # Interface for ProbeEndstopWrapper
    def probing_move(self, pos, speed):
        # Perform probing move
        phoming = self._printer.lookup_object('homing')
        trig_pos = phoming.probing_move(self, pos, speed)
        if not self._trigger_time:
            return trig_pos
        # Wait for 200ms to elapse since trigger time
        reactor = self._printer.get_reactor()
        while 1:
            systime = reactor.monotonic()
            est_print_time = self._mcu.estimated_print_time(systime)
            need_delay = self._trigger_time + 0.200 - est_print_time
            if need_delay <= 0.:
                break
            reactor.pause(systime + need_delay)
        # Find position since trigger
        samples = self._samples
        self._samples = []
        start_time = self._trigger_time + 0.050
        end_time = start_time + 0.100
        samp_sum = 0.
        samp_count = 0
        for msg in samples:
            data = msg['data']
            if data[0][0] > end_time:
                break
            if data[-1][0] < start_time:
                continue
            for time, freq, z in data:
                if time >= start_time and time <= end_time:
                    samp_sum += z
                    samp_count += 1
        if not samp_count:
            raise self._printer.command_error(
                "Unable to obtain probe_eddy_current sensor readings")
        halt_z = samp_sum / samp_count
        # Calculate reported "trigger" position
        toolhead = self._printer.lookup_object("toolhead")
        new_pos = toolhead.get_position()
        new_pos[2] += self._z_offset - halt_z
        return new_pos
    def multi_probe_begin(self):
        if not self._calibration.is_calibrated():
            raise self._printer.command_error(
                "Must calibrate probe_eddy_current first")
        self._start_measurements()
    def multi_probe_end(self):
        self._stop_measurements()
    def probe_prepare(self, hmove):
        pass
    def probe_finish(self, hmove):
        pass
    def get_position_endstop(self):
        return self._z_offset

# Main "printer object"
class PrinterEddyProbe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.calibration = EddyCalibration(config)
        # Sensor type
        sensors = { "ldc1612": ldc1612.LDC1612 }
        sensor_type = config.getchoice('sensor_type', {s: s for s in sensors})
        self.sensor_helper = sensors[sensor_type](config, self.calibration)
        # Probe interface
        self.probe = EddyEndstopWrapper(config, self.sensor_helper,
                                        self.calibration)
        self.printer.add_object('probe', probe.PrinterProbe(config, self.probe))
    def add_client(self, cb):
        self.sensor_helper.add_client(cb)
    def stream(self, cb):
        return StreamingContext(self, cb)

DRIFT_SAMPLE_COUNT = 9

class EddyDriftCompensation:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.drift_calibration = None
        self.calibration_samples = None
        self.scale = config.getfloat("drift_adjust_factor", 1., minval=0.)
        self.dc_min_temp = config.getfloat("drift_calibration_min_temp", 0.)
        dc = config.getlists(
            "drift_calibration", None, seps=(',', '\n'), parser=float
        )
        self.min_freq = 999999999999.
        if dc is not None:
            for coefs in dc:
                if len(coefs) != 3:
                    raise config.error(
                        "Invalid polynomial in drift calibration"
                    )
            self.drift_calibration = [Polynomial2d(*coefs) for coefs in dc]
            cal = self.drift_calibration
            self._check_calibration(cal, self.dc_min_temp, config.error)
            low_poly = self.drift_calibration[-1]
            self.min_freq = min([low_poly(temp) for temp in range(121)])
            cal_str = "\n".join([repr(p) for p in cal])
            logging.info(
                "%s: loaded temperature drift calibration. Min Temp: %.2f,"
                " Min Freq: %.6f\n%s"
                % (self.name, self.dc_min_temp, self.min_freq, cal_str)
            )
        else:
            logging.info(
                "%s: No drift calibration configured, disabling temperature "
                "compensation"
                % (self.name,)
            )
        self.enabled = has_dc = self.drift_calibration is not None
        cal_temp = config.getfloat('calibration_temp', 0)
        if cal_temp < 1e-6 and has_dc:
            self.enabled = False
            logging.info(
                "%s: No temperature saved for eddy probe calibration, "
                "disabling temperature compensation."
                % (self.name,)
            )

        short_name = self.name.split(maxsplit=1)[-1]
        temp_section = "temperature_probe " + short_name
        self.temp_sensor = None
        if config.has_section(temp_section):
            self.temp_sensor = self.printer.load_object(
                config, temp_section
            )
            self.temp_sensor.register_calibration_helper(self)
        if self.temp_sensor is None and has_dc:
            self.enabled = False
            logging.info(
                "%s: Temperature Sensor [%s] not configured, "
                "disabling temperature compensation"
                % (self.name, temp_section)
            )

        # Register Gcode Command
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            "SET_PROBE_DRIFT_ADJ_FACTOR", "PROBE",
            short_name, self.cmd_SET_DRIFT_FACTOR,
            desc=self.cmd_SET_DRIFT_FACTOR_help
        )

    def is_enabled(self):
        return self.enabled

    def collect_sample(self, kin_pos, tool_zero_z, speeds):
        if self.calibration_samples is None:
            self.calibration_samples = [[] for _ in range(DRIFT_SAMPLE_COUNT)]
        move_times = []
        probe_samples = [[] for _ in range(DRIFT_SAMPLE_COUNT)]
        scanner = self.printer.lookup_object(self.name)
        toolhead = self.printer.lookup_object("toolhead")
        reactor = self.printer.get_reactor()
        cur_pos = toolhead.get_position()
        sample_temp = self.get_temperature()
        lift_speed, probe_speed, _ = speeds

        def _on_data_recd(msg):
            data = msg["data"]
            if not move_times:
                return
            idx, start_time, end_time = move_times[0]
            for sample in data:
                ptime = sample[0]
                while ptime > end_time:
                    move_times.pop(0)
                    if not move_times:
                        return
                    idx, start_time, end_time = move_times[0]
                if ptime < start_time:
                    continue
                probe_samples[idx].append(sample)
        with scanner.stream(_on_data_recd):
            for i in range(DRIFT_SAMPLE_COUNT):
                if i == 0:
                    # Move down to first sample location
                    cur_pos[2] = tool_zero_z + .05
                else:
                    # Sample each .5mm in z
                    cur_pos[2] += 1.
                    toolhead.manual_move(cur_pos, lift_speed)
                    cur_pos[2] -= .5
                toolhead.manual_move(cur_pos, probe_speed)
                start = toolhead.get_last_move_time() + .05
                end = start + .1
                move_times.append((i, start, end))
                toolhead.dwell(.2)
            toolhead.wait_moves()
            sample_temp = (sample_temp + self.get_temperature()) / 2.
            reactor.pause(reactor.monotonic() + .2)
        for i, data in enumerate(probe_samples):
            freqs = [d[1] for d in data]
            zvals = [d[2] for d in data]
            avg_freq = sum(freqs) / len(freqs)
            avg_z = sum(zvals) / len(zvals)
            kin_z = i * .5 + .05 + kin_pos[2]
            logging.info(
                "Probe Values at Temp %.2fC, Z %.4fmm: Avg Freq = %.6f, "
                "Avg Measured Z = %.6f"
                % (sample_temp, kin_z, avg_freq, avg_z)
            )
            self.calibration_samples[i].append((sample_temp, avg_freq))
        return sample_temp

    def start_calibration(self):
        self.calibration_samples = [[] for _ in range(DRIFT_SAMPLE_COUNT)]

    def finish_calibration(self, success):
        cal_samples = self.calibration_samples
        self.calibration_samples = None
        if not success:
            return
        gcode = self.printer.lookup_object("gcode")
        if len(cal_samples) < 3:
            raise gcode.error(
                "calbration error, not enough samples"
            )
        min_temp, _ = cal_samples[0][0]
        polynomials = []
        for i, coords in enumerate(cal_samples):
            height = .05 + i * .5
            poly = Polynomial2d.fit(coords)
            polynomials.append(poly)
            logging.info("Polynomial at Z=%.2f: %s" % (height, repr(poly)))
        self._check_calibration(polynomials, min_temp)
        coef_cfg = "\n" + "\n".join([str(p) for p in polynomials])
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.name, "drift_calibration", coef_cfg)
        configfile.set(self.name, "drift_calibration_min_temp", min_temp)
        gcode.respond_info(
            "%s: generated %d 2D polynomials\n"
            "The SAVE_CONFIG command will update the printer config "
            "file and restart the printer."
            % (self.name, len(polynomials))
        )
        try:
            # Dump collected data to temporary file
            import json
            ctime = int(self.printer.get_reactor().monotonic())
            tmpfname = "/tmp/eddy-probe-drift-%d.json" % (ctime)
            out = {
                "polynomial_coefs": [c.get_coefs() for c in polynomials],
                "legend": ["temperature", "frequency"],
                "data": cal_samples,
                "start_z": .05,
                "sample_z_dist": .5
            }
            with open(tmpfname, "w") as f:
                f.write(json.dumps(out))
        except Exception:
            logging.exception("Failed to write %s" % (tmpfname))

    def _check_calibration(self, calibration, start_temp, error=None):
        error = error or self.printer.command_error
        start = int(start_temp)
        for temp in range(start, 121, 1):
            last_freq = calibration[0](temp)
            for i, poly in enumerate(calibration[1:]):
                next_freq = poly(temp)
                if next_freq >= last_freq:
                    # invalid polynomial
                    raise error(
                        "%s: invalid calibration detected, curve at index "
                        "%d overlaps previous curve at temp %dC."
                        % (self.name, i + 1, temp)
                    )
                last_freq = next_freq

    def adjust_freq(self, freq, dest_temp):
        # Adjusts frequency from current temperature toward
        # destination temperature
        if not self.enabled or freq < self.min_freq:
            return freq
        cur_temp = self.temp_sensor.get_temp()[0]
        return self._calc_freq(freq, cur_temp, dest_temp)

    def lookup_freq(self, freq, origin_temp):
        # Given a frequency and its orignal sampled temp, find the
        # offset frequency based on the current temp
        if not self.enabled or freq < self.min_freq:
            return freq
        cur_temp = self.temp_sensor.get_temp()[0]
        return self._calc_freq(freq, origin_temp, cur_temp)

    def _calc_freq(self, freq, origin_temp, dest_temp):
        high_freq = low_freq = None
        dc = self.drift_calibration
        for pos, poly in enumerate(dc):
            high_freq = low_freq
            low_freq = poly(origin_temp)
            if freq >= low_freq:
                if high_freq is None:
                    # Freqency above max calibration value
                    return freq + ((dc[0](dest_temp) - freq) * self.scale)
                t = min(1., max(0., (freq - low_freq) / (high_freq - low_freq)))
                low_tgt_freq = poly(dest_temp)
                high_tgt_freq = dc[pos-1](dest_temp)
                err = ((1 - t) * low_tgt_freq + t * high_tgt_freq) - freq
                return freq + err * self.scale
        # Frequency below minimum, no correction
        return freq

    def get_temperature(self):
        if self.temp_sensor is not None:
            return self.temp_sensor.get_temp()[0]
        return 0.

    cmd_SET_DRIFT_FACTOR_help = (
        "Set adjustment factor applied to drift correction"
    )
    def cmd_SET_DRIFT_FACTOR(self, gcmd):
        self.scale = gcmd.get_float("FACTOR", minval=0.)

class StreamingContext:
    def __init__(self, scanning_probe, callback):
        self.scanner = scanning_probe
        self.callback = callback
        self.reactor = scanning_probe.printer.get_reactor()
        self.done_completion = None
        self.stream_enabled = False

    def __enter__(self):
        self.stream_enabled = True
        self.scanner.add_client(self._bulk_callback)

    def __exit__(self, type=None, value=None, tb=None):
        self.done_completion = self.reactor.completion()
        self.stream_enabled = False
        self.done_completion.wait()
        self.done_completion = None

    def _bulk_callback(self, msg):
        if not self.stream_enabled:
            if self.done_completion is not None:
                self.done_completion.complete(None)
            return False
        self.callback(msg)
        return True


MAX_HIT_DIST = 2.
MM_WIN_SPEED = 125

class SamplingMode:
    STANDARD = 0
    WEIGHTED = 1
    CENTERED = 2
    LINEAR = 3

    @staticmethod
    def from_str(sampling_type):
        return getattr(SamplingMode, sampling_type.upper(), 0)

    def to_str(sampling_idx):
        return {
            val: name.lower() for name, val in SamplingMode.__dict__.items()
            if name[0].isupper() and isinstance(val, int)
        }.get(sampling_idx, "standard")


class ProbeScanHelper:
    def __init__(
            self, printer, points, use_offsets, speed,
            horizontal_move_z, finalize_cb
    ):
        self.printer = printer
        self.points = points
        self.probe = self.printer.lookup_object("probe")
        self.probe_offsets = self.probe.get_offsets()
        self.use_offsets = use_offsets
        self.speed = speed
        self.scan_height = horizontal_move_z
        self.finalize_callback = finalize_cb
        self.min_time_window = 0
        self.max_time_window = 0
        self.sampling_mode = SamplingMode.STANDARD
        self.reset()

    def reset(self):
        self.samples_pending = []
        self.lookahead_index = 0
        self.sample_index = 0

    def perform_scan(self, gcmd):
        mode = gcmd.get("SCAN_MODE", "detailed")
        speed = gcmd.get_float("SCAN_SPEED", self.speed)
        default_sm = "centered" if mode == "detailed" else "standard"
        sampling_mode = gcmd.get("SAMPLES_RESULT", default_sm)
        self.sampling_mode = SamplingMode.from_str(sampling_mode)
        gcmd.respond_info(
            "Beginning %s surface scan at height %.2f, sampling mode %s..."
            % (mode, self.scan_height, SamplingMode.to_str(self.sampling_mode))
        )
        while True:
            self.reset()
            if mode == "rapid":
                results = self._rapid_scan(gcmd, speed)
            else:
                results = self._detailed_scan(gcmd, speed)
            # There is no z_offset since the scan height is used
            # to calculate the "probed" position
            offsets = list(self.probe_offsets)
            offsets[2] = 0
            ret = self.finalize_callback(tuple(offsets), results)
            if ret != "retry":
                break

    def _detailed_scan(self, gcmd, speed):
        scanner = self.printer.lookup_object(self.probe.get_probe_name())
        toolhead = self.printer.lookup_object("toolhead")
        sample_time = gcmd.get_float("SAMPLE_TIME", .1, above=.1)
        sample_time += .05
        self._raise_tool(gcmd)
        # Start sampling and go
        reactor = self.printer.get_reactor()
        self.min_time_window = -0.05
        self.max_time_window = sample_time
        with scanner.stream(self._on_bulk_data_collected):
            for idx, pos in enumerate(self.points):
                pos = self._apply_offsets(pos[:2])
                toolhead.manual_move(pos, speed)
                if idx == 0:
                    self._move_to_scan_height(gcmd)
                toolhead.register_lookahead_callback(self._lookahead_callback)
                toolhead.dwell(sample_time)
            toolhead.wait_moves()
            reactor.pause(reactor.monotonic() + .2)
        return self._process_batch_measurements(gcmd)

    def _rapid_scan(self, gcmd, speed):
        scanner = self.printer.lookup_object(self.probe.get_probe_name())
        toolhead = self.printer.lookup_object("toolhead")
        # Calculate time window around which a sample is valid.  Current
        # assumption is anything within 2mm is usable, so:
        # window = 2 / max_speed
        #
        # TODO: validate maximum speed allowed based on sample rate of probe
        # Scale the hit distance window for speeds lower than 125mm/s.  The
        # lower the speed the less the window shrinks.
        scale = max(0, 1 - speed / MM_WIN_SPEED) + 1
        hit_dist = min(MAX_HIT_DIST, scale * speed / MM_WIN_SPEED)
        window = hit_dist / speed
        gcmd.respond_info(
            "Sample hit distance +/- %.4fmm, time window +/- ms %.4f"
            % (hit_dist, window * 1000)
        )
        self.min_time_window = self.max_time_window = window
        self._raise_tool(gcmd)
        # Start sampling and go
        reactor = self.printer.get_reactor()
        with scanner.stream(self._on_bulk_data_collected):
            ptgen = getattr(self.points, "iter_rapid", self._rapid_default_gen)
            for idx, (pos, is_probe_pt) in enumerate(ptgen()):
                pos = self._apply_offsets(pos[:2])
                toolhead.manual_move(pos, speed)
                if idx == 0:
                    self._move_to_scan_height(gcmd)
                if is_probe_pt:
                    toolhead.register_lookahead_callback(
                        self._lookahead_callback
                    )
            toolhead.wait_moves()
            reactor.pause(reactor.monotonic() + .2)
        return self._process_batch_measurements(gcmd)

    def _rapid_default_gen(self):
        for pt in self.points:
            yield pt, True

    def _raise_tool(self, gcmd):
        # If the nozzle is below scan height raise the tool
        toolhead = self.printer.lookup_object("toolhead")
        cur_pos = toolhead.get_position()
        if cur_pos[2] >= self.scan_height:
            return
        lift_speed = self.probe.get_lift_speed(gcmd)
        cur_pos[2] = self.scan_height + .5
        toolhead.manual_move(cur_pos, lift_speed)

    def _move_to_scan_height(self, gcmd):
        toolhead = self.printer.lookup_object("toolhead")
        cur_pos = toolhead.get_position()
        lift_speed = self.probe.get_lift_speed(gcmd)
        probe_speed = self.probe.get_probe_speed(gcmd)
        cur_pos[2] = self.scan_height + .5
        toolhead.manual_move(cur_pos, lift_speed)
        cur_pos[2] = self.scan_height
        toolhead.manual_move(cur_pos, probe_speed)
        toolhead.dwell(abs(self.min_time_window) + .01)

    def _apply_offsets(self, point):
        if self.use_offsets:
            return [(pos - ofs) for pos, ofs in zip(point, self.probe_offsets)]
        return point

    def _lookahead_callback(self, print_time):
        pt = self._apply_offsets(self.points[self.lookahead_index])
        self.lookahead_index += 1
        self.samples_pending.append((print_time, pt, []))

    def _on_bulk_data_collected(self, msg):
        idx = self.sample_index
        if len(self.samples_pending) < idx + 1:
            return
        req_time, _, z_samples = self.samples_pending[idx]
        data = msg["data"]
        for ptime, _, z in data:
            while ptime > req_time + self.max_time_window:
                self.sample_index += 1
                idx = self.sample_index
                if len(self.samples_pending) < idx + 1:
                    return
                req_time, _, z_samples = self.samples_pending[idx]
            if ptime < req_time - self.min_time_window:
                continue
            z_samples.append((ptime, z))

    def _process_batch_measurements(self, gcmd):
        results = []
        if len(self.samples_pending) != len(self.points):
            raise gcmd.error(
                "Position sample length does not match number of points "
                "requested, received: %d, requested %d"
                % (len(self.samples_pending), len(self.points))
            )
        last_sample = (0, 0, [])
        overlaps = set()
        for idx, sample in enumerate(self.samples_pending):
            # look behind for overlapping samples
            min_smample_time = sample[0] - self.min_time_window
            z_samples = sample[2]
            for zs in last_sample[2]:
                if zs in z_samples:
                    continue
                if zs[0] >= min_smample_time:
                    z_samples.append(zs)
                    overlaps.add(idx)
            z_samples.sort(key=lambda s: s[0])
            results.append(self._process_samples(gcmd, sample))
            last_sample = sample
        if overlaps:
            logging.info("Detected %d overlapping samples" % (len(overlaps),))
        return results

    def _process_samples(self, gcmd, sample):
        move_time, pos, z_samples = sample
        valid_samples = []
        for sample in z_samples:
            if abs(sample[1]) > 99.0:
                logging.info(
                    "Z-sample at time %.6f for position (%.6f, %.6f) is "
                    "invalid.  Verify that 'horizontal_move_z' not too high."
                    % (sample[0], pos[0], pos[1])
                )
            else:
                valid_samples.append(sample)
        sample_len = len(valid_samples)
        if sample_len == 0:
            raise gcmd.error(
                "No valid measurements found at coordinate : %s"
                % (repr(pos),)
            )
        if self.sampling_mode == SamplingMode.CENTERED:
            # Average middle samples
            z_vals = sorted([s[1] for s in valid_samples])
            discard_count = len(z_vals) // 4
            keep_vals = z_vals[discard_count:-discard_count]
            sample_len = len(keep_vals)
            z_height = sum(keep_vals) / sample_len
        elif self.sampling_mode == SamplingMode.WEIGHTED:
            # Perform a weighted average
            time_diffs = [abs(move_time - s[0]) for s in valid_samples]
            max_diff = max(time_diffs)
            # Add weight as a factor of the furthest sample
            weights = [max_diff / diff for diff in time_diffs]
            total_weight = sum(weights)
            z_height = 0
            for weight, sample in zip(weights, valid_samples):
                factor = weight / total_weight
                z_height += sample[1] * factor
        elif self.sampling_mode == SamplingMode.LINEAR:
            sample_times = [s[0] for s in valid_samples]
            idx = bisect.bisect(sample_times, move_time)
            if idx == 0:
                z_height = valid_samples[0][1]
            elif idx == sample_len:
                z_height = valid_samples[-1][1]
            else:
                min_time, min_z = valid_samples[idx - 1]
                max_time, max_z = valid_samples[idx]
                time_diff = max_time - min_time
                time_from_min = move_time - min_time
                t = min(1, max(0, time_from_min / time_diff))
                z_height = (1. - t) * min_z + t * max_z
        else:
            z_height = sum([s[1] for s in valid_samples]) / sample_len
        probed_z = self.scan_height - z_height
        atc = self.printer.lookup_object("axis_twist_compensation", None)
        if atc is not None:
            # perform twist compensation on the desired sample position
            z_comp = atc.get_z_compensation_value(pos)
            probed_z += z_comp
        logging.info(
            "Scan at (%.4f, %.4f) is z=%.6f, height=%.6f, sample count=%d"
            % (pos[0], pos[1], probed_z, z_height, sample_len)
        )
        return [pos[0], pos[1], probed_z]

def load_config_prefix(config):
    return PrinterEddyProbe(config)
