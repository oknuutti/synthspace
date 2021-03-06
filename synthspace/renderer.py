from __future__ import annotations

import datetime
from pathlib import Path

import math
import os

import numpy as np
import quaternion
import cv2

from visnav.algo.model import Camera
from visnav.iotools.objloader import ShapeModel
from visnav.algo import tools
from visnav.missions.rosetta import ChuryumovGerasimenko
from visnav.render.render import RenderEngine
from visnav.testloop import TestLoop


class RenderControllerError(RuntimeError):
    """Generic error for RenderController."""
    pass


class RenderAbstractObject:
    def __init__(self, name):
        self.name = name
        self._dirty = True

    def is_dirty(self):
        return self._dirty

    def set_dirty(self):
        self._dirty = True

    def clear_dirty(self):
        self._dirty = False


class RenderCamera(RenderAbstractObject):
    DEFAULTS = {
        'f_stop': 2.2,
        'emp_coef': 1/100,          # attenuating filter, similar like on Rosetta NavCam
        'quantum_eff': 0.5,
        'px_saturation_e': 13.5e3,
        'lambda_min': 350e-9,
        'lambda_eff': 580e-9,
        'lambda_max': 800e-9,
        'dark_noise_mu': 110,
        'readout_noise_sd': 15,
        'point_spread_fn': 0.5,
        'scattering_coef': 2e-9,
        'exclusion_angle_x': 45,
        'exclusion_angle_y': 45,
    }

    def __init__(self, name):
        super().__init__(name)
        self.model = None
        self.exposure = 1
        self.gain = 1
        self.loc = None
        self.q = None
        self.target_axis = (0, 0, -1)           # camera boresight
        self.target_axis_up = (0, 1, 0)         # up direction of camera boresight
        self.target = None                      # target object
        self.target_up = (0, 1, 0)              # target up direction

        self.focal_length = None
        self.sensor_width = None
        self.frustum_near = None
        self.frustum_far = None
        self.extra = None

    def conf(self, lens, sensor, clip_start, clip_end, **extra):
        self.set_dirty()
        self.focal_length = lens
        self.sensor_width = sensor
        self.frustum_near = clip_start
        self.frustum_far = clip_end
        self.extra = extra

    def is_dirty(self):
        return super().is_dirty() or self.model is None

    def prepare(self, scene):
        w, h = scene.width, scene.height

        if self.target is not None:
            # change orientation so that target is on the camera bore-sight
            self._update_target()

        if self.is_dirty() or self.model.width != w or self.model.height != h:
            x_fov = math.degrees(2 * math.atan(self.sensor_width/2/self.focal_length))
            y_fov = x_fov * h/w
            params = RenderCamera.DEFAULTS.copy()
            params.update(self.extra)
            if 'aperture' in params:
                params.pop('f_stop', False)
            if 'px_saturation_e' not in self.extra:
                params['px_saturation_e'] *= ((self.sensor_width/w)/5.5e-3)**2
            if 'dark_noise_mu' not in self.extra:
                params['dark_noise_mu'] *= params['px_saturation_e']/self.DEFAULTS['px_saturation_e']
            if 'dark_noise_sd' not in self.extra:
                params['dark_noise_sd'] = np.sqrt(params['dark_noise_mu'])

            self.model = Camera(w, h, x_fov, y_fov, focal_length=self.focal_length, **params)
            self.clear_dirty()

    def _check_params(self):
        assert self.loc is not None, 'Location not set for camera %s' % self.name
        assert self.q is not None or self.target is not None, 'Orientation or target is not set for camera %s' % self.name
        assert self.focal_length is not None, 'Camera %s not configured' % self.name
        assert self.sensor_width is not None, 'Camera %s not configured' % self.name
        assert self.frustum_near is not None, 'Camera %s not configured' % self.name
        assert self.frustum_far is not None, 'Camera %s not configured' % self.name

    def _update_target(self):
        """
        Change camera orientation so that target is on the camera bore-sight defined by target_axis vector.
        Additional constraint is needed for unique final rotation, this can be provided by target_up vector.
        """
        boresight = np.array(self.target_axis)
        loc = self.target.loc - self.loc
        axis = np.cross(boresight, loc)
        angle = tools.angle_between_v(boresight, loc)
        q = tools.angleaxis_to_q((angle,) + tuple(axis))

        # if up target given, use it
        if self.target_up is not None:
            current_up = tools.q_times_v(q, np.array(self.target_axis_up))
            target_up = np.array(self.target_up)

            # project target_up on camera bore-sight, then remove the projection from target_up to get
            # it projected on a plane perpendicular to the bore-sight
            target_up_proj = target_up - np.dot(target_up, loc) * loc / np.dot(loc, loc)
            if np.linalg.norm(target_up_proj) > 0:
                axis = np.cross(target_up_proj, current_up)
                angle = tools.angle_between_v(current_up, target_up_proj)
                q = tools.angleaxis_to_q((angle,) + tuple(axis)) * q

        self.q = q


class RenderObject(RenderAbstractObject):
    HAPKE_PARAMS = ChuryumovGerasimenko.HAPKE_PARAMS

    def __init__(self, name, data):
        super().__init__(name)
        self.model = data
        self.clear_dirty()
        self.rotation_mode = None   # not used
        self.loc = None
        self.q = None

    @property
    def location(self):
        return tuple(self.loc)

    @location.setter
    def location(self, value):
        self.loc = np.array(value)

    @property
    def rotation_axis_angle(self):
        return tuple(tools.q_to_angleaxis(self.q)) if self.q else None

    @rotation_axis_angle.setter
    def rotation_axis_angle(self, angleaxis):
        self.q = tools.angleaxis_to_q(angleaxis)

    @property
    def rotation_angleaxis(self):                       # better name as angle given first, then axis
        return tuple(tools.q_to_angleaxis(self.q)) if self.q else None

    @rotation_angleaxis.setter
    def rotation_angleaxis(self, angleaxis):            # better name as angle given first, then axis
        self.q = tools.angleaxis_to_q(angleaxis)

    def prepare(self, scene):
        self._check_params()
        if self.is_dirty():
            # seems no need to do any preparations before rendering
            self.clear_dirty()

    def _check_params(self):
        assert self.loc is not None, 'Location not set for object %s' % self.name
        assert self.q is not None, 'Orientation not set for object %s' % self.name


class RenderScene(RenderAbstractObject):
    STAR_DB_URL = 'https://drive.google.com/uc?authuser=0&id=1-_7KAMKc4Xio0RbpiVWmcPSNyuuN8Z2b&export=download'
    STAR_DB = Path(os.path.join(os.path.dirname(__file__), '..', 'data', 'deep_space_objects.sqlite'))

    def __init__(self, name, render_dir, stars=True, lens_effects=False, flux_only=False, normalize=False,
                 hapke_params=RenderObject.HAPKE_PARAMS, verbose=True, debug=False):

        super().__init__(name)
        self._samples = 1
        self._width = None
        self._height = None

        self._render_dir = str(render_dir)
        self._file_format = None
        self._color_depth = None
        self._use_preview = None

        self._cams = {}
        self._objs = {}
        self._sun_loc = None
        self._renderer = None

        self.object_scale = 1000   # objects given in km, locations expected in meters
        self.flux_only = flux_only
        self.normalize = normalize
        self.stars = stars
        self.lens_effects = lens_effects
        self.hapke_params = hapke_params
        self.verbose = verbose
        self.debug = debug

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    def is_dirty(self):
        return super().is_dirty() or np.any([i is None for i, o in self._objs.values()])

    def prepare(self):
        self._check_params()

        if self.is_dirty():
            if self._renderer is not None:
                del self._renderer

            self._renderer = RenderEngine(self._width, self._height, antialias_samples=self._samples)
            if self.verbose:
                print('loading objects to engine...', end='', flush=True)

            for name, (_, obj) in self._objs.items():
                idx = self._renderer.load_object(obj.model)
                self._objs[name][0] = idx

            self.clear_dirty()
            if self.verbose:
                print('done')
        if self.stars:
            if not os.path.exists(RenderScene.STAR_DB):
                if self.verbose:
                    print('downloading star catalog...', end='', flush=True)
                RenderController.download_file(RenderScene.STAR_DB_URL, RenderScene.STAR_DB)
                if self.verbose:
                    print('done')

    def render(self, name_suffix):
        self.prepare()
        for i, o in self._objs.values():
            o.prepare(self)
        for c in self._cams.values():
            c.prepare(self)

        sun_sc_v = np.mean(np.array([o.loc - self._sun_loc for _, o in self._objs.values()]).reshape((-1, 3)), axis=0)
        sun_distance = np.linalg.norm(sun_sc_v)
        obj_idxs = [i for i, o in self._objs.values()]

        for cam_name, c in self._cams.items():
            rel_pos_v = {}
            rel_rot_q = {}
            for i, o in self._objs.values():
                rel_pos_v[i] = tools.q_times_v(c.q.conj(), o.loc - c.loc)
                rel_rot_q[i] = c.q.conj() * o.q

            # make sure correct order, correct scale
            rel_pos_v = [rel_pos_v[i]/self.object_scale for i in obj_idxs]
            rel_rot_q = [rel_rot_q[i] for i in obj_idxs]
            light_v = tools.q_times_v(c.q.conj(), tools.normalize_v(sun_sc_v))

            self._renderer.set_frustum(c.model.x_fov, c.model.y_fov, c.frustum_near, c.frustum_far)
            flux = TestLoop.render_navcam_image_static(None, self._renderer, obj_idxs, rel_pos_v, rel_rot_q,
                                                       light_v, c.q, sun_distance, cam=c.model, auto_gain=False,
                                                       use_shadows=True, use_textures=True, fluxes_only=True,
                                                       stars=self.stars, lens_effects=self.lens_effects,
                                                       reflmod_params=self.hapke_params, star_db=RenderScene.STAR_DB)

            image = flux if self.flux_only else c.model.sense(flux, exposure=c.exposure, gain=c.gain)

            if self.normalize:
                image /= np.max(image)

            if self.debug:
                sc = 1536/image.shape[0]
                img = cv2.resize(image, None, fx=sc, fy=sc) / (np.max(image) if self.flux_only else 1)
                cv2.imshow('result', img)
                cv2.waitKey()

            # save image
            self._save_img(image, cam_name, name_suffix)

    def _check_params(self):
        assert self._sun_loc is not None, 'Sun location not set for scene %s' % self.name
        assert self._width is not None, 'Common camera resolution width not set for scene %s' % self.name
        assert self._height is not None, 'Common camera resolution height not set for scene %s' % self.name
        assert self._file_format is not None, 'Output file format not set for scene %s' % self.name
        assert self._color_depth is not None, 'Output file format not set for scene %s' % self.name
        assert self._use_preview is not None, 'Output file format not set for scene %s' % self.name
        assert len(self._cams) > 0, 'Scene %s does not have any cameras' % self.name
        assert len(self._objs) > 0, 'Scene %s does not have any objects' % self.name

    def _save_img(self, image, cam_name, name_suffix):
        file_ext = '.exr' if self._file_format == RenderController.FORMAT_EXR else '.png'
        filename = os.path.join(self._render_dir, self.name + "_" + cam_name + "_" + name_suffix + file_ext)

        if self._file_format == RenderController.FORMAT_PNG:
            maxval = self._color_depth ** 2 - 1
            image = np.clip(image * maxval, 0, maxval).astype('uint' + str(self._color_depth))
            cv2.imwrite(filename, image)
        else:
            cv2.imwrite(filename, image.astype(np.float32), (cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_FLOAT))

    def set_samples(self, samples):
        supported = (1, 4, 9, 16)
        assert samples in supported, '%s samples are not supported, only the following are: %s' % (samples, supported)
        if self._samples != samples:
            self.set_dirty()
            self._samples = samples

    def set_resolution(self, res):
        if (self._width, self._height) != tuple(res):
            self._width, self._height = res
            self.set_dirty()
            for c in self._cams.values():
                c.set_dirty()

    def set_output_format(self, file_format, color_depth, use_preview):
        self._file_format = file_format
        self._color_depth = color_depth
        self._use_preview = use_preview

    def link_camera(self, cam: RenderCamera):
        self._cams[cam.name] = cam

    def link_object(self, obj: RenderObject):
        self._objs[obj.name] = [None, obj]

    def set_sun_location(self, loc):
        """
        :param loc: sun location in meters in the same frame (e.g. asteroid/comet centric) used for camera and object locations
        """
        self._sun_loc = np.array(loc)


class RenderController:
    """Class to control synthetic image generation."""

    (
        FORMAT_EXR,
        FORMAT_PNG,
    ) = range(2)

    SOL = RenderObject('Sol', None)

    def __init__(self, render_dir, logger=None, verbose=True):
        """Initialize controller class."""
        self._render_dir = render_dir
        self._scenes = {}
        self._cams = {}
        self._objs = {}
        self._logger = logger
        self.verbose = verbose

    def create_scene(self, name):
        """Add empty scene."""
        self._scenes[name] = RenderScene(name, render_dir=self._render_dir, verbose=self.verbose)

    def set_scene_defaults(self, scenes=None):
        """
        Sets default settings to a scene.
        WILL NOT IMPLEMENT
        """

    def set_scene_config(self, params: dict, scenes=None):
        """
        Set config params for scene(s)
        """
        for s in self._iter_scenes(scenes):
            for p, v in params.items():
                assert p in s.__dict__, "Class RenderScene does not have a property with the name '%s'" % p
                setattr(s, p, v)

    def set_device(self, device="AUTO", scenes=None):
        """
        Set rendering device for given scenes.
        WILL NOT IMPLEMENT, renderer doesn't support specifying the device
        """

    def get_tile_size(self):
        assert False, 'this should not be a public method'

    def set_samples(self, samples=4, scenes=None):
        """Set number of samples to render for each pixel."""
        for s in self._iter_scenes(scenes):
            s.set_samples(samples)

    def set_exposure(self, exposure, cameras=None):
        """Set exposure value."""
        # TODO: (X) should set exposure on cameras right? in ref impl camera param is called scenes instead
        for c in self._iter_cams(cameras):
            c.exposure = exposure

    def set_resolution(self, res, scenes=None):
        """Sets resolution of rendered image."""
        # TODO: (X) logically this should be set on cameras, however due to rendering specifics, easier to set on scene
        for s in self._iter_scenes(scenes):
            s.set_resolution(res)

    def set_output_format(self,
                          file_format="OPEN_EXR",
                          color_depth="32",
                          use_preview=True,
                          scenes=None):
        """Set output file format, supports OPEN_EXR or PNG."""
        color_depth = int(color_depth)
        file_format = {
            "OPEN_EXR": RenderController.FORMAT_EXR,
            "PNG": RenderController.FORMAT_PNG
        }.get(file_format.upper(), None)
        assert file_format is not None, 'only OPEN_EXR and PNG supported'
        assert (file_format == RenderController.FORMAT_PNG and color_depth in (8, 16) or
                file_format == RenderController.FORMAT_EXR and color_depth == 32), \
                'PNG supports color depths 8 and 16, OPEN_EXR a color depth of 32'
        for s in self._iter_scenes(scenes):
            s.set_output_format(file_format, color_depth, use_preview)

    def set_output_file(self, name_suffix=None, scene=None):
        """Set output file path to given scenes with prior extension check."""
        assert False, 'this should not be a public method'

    def create_camera(self, camera_name="Camera", scenes=None):
        """Create new camera and add to relevant scenes."""
        self._cams[camera_name] = RenderCamera(camera_name)
        for s in self._iter_scenes(scenes):
            s.link_camera(self._cams[camera_name])

    def configure_camera(self,
                         camera_name="Camera",
                         lens=35.0,
                         sensor=32.0,
                         clip_start=1E-2,
                         clip_end=1E12,
                         mode="PERSP",  # Modes ORTHO, PERSP
                         ortho_scale=7.0, **kwargs):
        """
        Set camera configuration values.
        lens: focal length in mm?
        sensor: sensor width in mm?
        """
        assert mode == "PERSP", 'ORTHO currently not supported even though it could be'
        self._cams[camera_name].conf(lens, sensor, clip_start, clip_end, **kwargs)

    def set_camera_location(self, camera_name="Camera", location=(0, 0, 0), orientation=None, angleaxis=True):
        cam = self._cams[camera_name]
        cam.loc = np.array(location) if location is not None else None
        if orientation is not None:
            if len(orientation) == 4:
                if angleaxis:
                    orientation = tools.angleaxis_to_q(orientation)
                else:
                    orientation = np.quaternion(*orientation)
            assert isinstance(orientation, np.quaternion), 'orientation needs to be a quaternion or angle-axis with angle given last'
            cam.q = orientation

    def target_camera(self, target_obj: RenderObject, camera_name="Camera"):
        """Target camera towards target."""
        self._cams[camera_name].target = target_obj

    def set_sun_location(self, loc, scenes=None):
        RenderController.SOL.location = loc
        for s in self._iter_scenes(scenes):
            s.set_sun_location(loc)

    def update(self, scenes=None):
        assert False, 'this should not be a public method'

    def render(self, metadata, scenes=None):
        """Render given scenes."""
        assert isinstance(metadata, dict), 'metadata dictionary needs to be given as a dictionary'
        assert "date" in  metadata, 'metadata needs to contain a "date" field'
        for s in self._iter_scenes(scenes):
            s.render(metadata["date"])

    def load_object(self, filename, object_name, scenes=None):
        """Load 3d model object from file."""
        filename = str(filename)
        assert filename[-4:].lower() == '.obj', 'only .obj files currently supported'
        if self.verbose:
            print('loading objects...', end='', flush=True)
        data = ShapeModel(fname=filename)
        obj = RenderObject(object_name, data)
        self._objs[object_name] = obj
        for s in self._iter_scenes(scenes):
            s.link_object(self._objs[object_name])
        if self.verbose:
            print('done')
        return obj

    def create_empty(self, name="Empty", scenes=None):
        assert False, 'seems like an unused / useless method'

    def save_blender_dfile(self, name_suffix=None, scene=None):
        assert False, 'this should not be a public method'

    def _iter_scenes(self, scenes):
        return self._iter(scenes, self._scenes)

    def _iter_cams(self, cams):
        return self._iter(cams, self._cams)

    def _iter(self, objs, all_objs):
        """Checks input to allow different types and create iterator.

        Input can either be None, a scene name (str), a list of scene names,
        a single scene, or a list of scenes.
        Output is an iterator which can be used for looping through scenes.
        """
        if objs is None:
            output = list(all_objs.values())
        elif isinstance(objs, str):
            output = [all_objs[objs]]
        elif isinstance(objs, RenderScene):
            output = [objs]
        elif isinstance(objs, list):
            if isinstance(objs[0], str):
                output = [all_objs[s] for s in objs]
            elif isinstance(objs[0], RenderScene):
                output = objs
            else:
                msg = f"Invalid input {objs}"
                self._log(msg)
                raise RenderControllerError(msg)
        else:
            msg = f"Invalid input {objs}"
            self._log(msg)
            raise RenderControllerError(msg)
        return iter(output)

    def _log(self, msg, level='info'):
        if self._logger is not None:
            method = getattr(self._logger, level, None) or 'print'
            getattr(self._logger, level)(msg)

    @staticmethod
    def download_file(url, file, maybe=False):
        file = str(file)
        if not maybe or not os.path.exists(file):
            import urllib
            os.makedirs(os.path.dirname(file), exist_ok=True)
            urllib.request.urlretrieve(url, file)


if __name__ == '__main__':
    target = ['sun', 'stars', 'sssb'][2]

    datapath = os.path.join(os.path.dirname(__file__), '..', 'data')
    outpath = os.path.join(os.path.dirname(__file__), '..', 'output')
    control = RenderController(outpath)
    control.create_scene('test_sc')
    control.set_scene_config({
        'debug': True,
        'flux_only': False,
        'normalize': False,
        'stars': True,
        'lens_effects': True,          # includes the sun
        'hapke_params': RenderObject.HAPKE_PARAMS,
    })
    control.create_camera('test_cam', scenes='test_sc')
    control.configure_camera('test_cam', lens=35.0, sensor=5e-3*1024)
    control.set_exposure(0.001 if target == 'sun' else 0.3 if target == 'sssb' else 50.0)
    control.set_resolution((1024, 1024))
    control.set_output_format('png', '8')
    control.set_sun_location(tools.q_times_v(tools.ypr_to_q(math.radians(-90+120), 0, 0), [1.496e11, 0, 0]))
    control.set_camera_location('test_cam')
    objfile1, url1 = os.path.join(datapath, 'ryugu+tex-d1-16k.obj'), 'https://drive.google.com/uc?authuser=0&id=1Lu48I4nnDKYtvArUN7TnfQ4b_MhSImKM&export=download'
    objfile2, url2 = os.path.join(datapath, 'ryugu+tex-d1-16k.mtl'), 'https://drive.google.com/uc?authuser=0&id=1qf0YMbx5nIceGETqhNqePyVNZAmqNyia&export=download'
    objfile3, url3 = os.path.join(datapath, 'ryugu.png'), 'https://drive.google.com/uc?authuser=0&id=19bT_Qd1MBfxM1wmnCgx6PT58ujnDFmsK&export=download'
    RenderController.download_file(url1, objfile1, maybe=True)
    RenderController.download_file(url2, objfile2, maybe=True)
    RenderController.download_file(url3, objfile3, maybe=True)
    obj = control.load_object(os.path.join(datapath, 'ryugu+tex-d1-16k.obj'), 'ryugu-16k')
    obj.location = (0, 0, 0)
    if target == 'sun':
        control.target_camera(control.SOL, "test_cam")
    elif target == 'sssb':
        control.target_camera(obj, "test_cam")
    else:
        control.set_camera_location("test_cam", None, (0, 1, 0, 0))
    start = datetime.datetime.now()

    for i in range(10):
        obj.rotation_axis_angle = (i/10 * np.pi/2, 0, 0, 1)
        control.set_camera_location("test_cam", i * np.array([0, -500, 0]) + np.array([0, 10000, 0]))
        control.render(datetime.datetime.strftime(start + datetime.timedelta(hours=i), '%Y%m%d_%H%M%S'))
