# coding: utf-8

import os
import os.path as osp
from copy import copy, deepcopy
import shutil

import cv2
import numpy as np
import tensorflow as tf
import supervisely_lib as sly
from supervisely_lib import logger

from model import ICNet_BN
from tools import preprocess, TrainConfigRW


class ICNetApplier:
    default_settings = {
        'gpu_devices': [0],
        'model_classes': {
            'save_classes': '__all__',
            'add_suffix': '_icnet',
        },
        'existing_objects': {
            'save_classes': [],
            'add_suffix': '',
        },
        'mode': {
            'source': 'full_image'
        },
    }
    default_settings_for_modes = {
        'full_image': {
            'source': 'full_image',
        },
        'roi': {
            'source': 'roi',
            'bounds': {
                'left': '0px',
                'top': '0px',
                'right': '0px',
                'bottom': '0px',
            },
            'save': False,
            'class_name': 'inference_roi'
        },
        'bboxes': {
            'source': 'bboxes',
            'from_classes': '__all__',
            'padding': {
                'left': '0px',
                'top': '0px',
                'right': '0px',
                'bottom': '0px',
            },
            'save': False,
            'add_suffix': '_input_bbox'
        },
        'sliding_window': {
            'source': 'sliding_window',
            'window': {
                'width': 128,
                'height': 128,
            },
            'min_overlap': {
                'x': 0,
                'y': 0,
            },
            'save': False,
            'class_name': 'sliding_window',
        },
    }

    def _determine_settings(self):
        input_config = self.helper.task_settings
        logger.info('Input config', extra={'config': input_config})

        config = deepcopy(self.default_settings)
        if 'mode' in input_config and 'source' in input_config['mode']:
            mode_name = input_config['mode']['source']
            default_mode_settings = self.default_settings_for_modes.get(mode_name, None)
            if default_mode_settings is not None:
                sly.update_recursively(config['mode'], deepcopy(default_mode_settings))
            # config now contains default values for selected mode

        sly.update_recursively(config, input_config)
        logger.info('Full config', extra={'config': config})
        # SettingsValidator.validate_inference_cfg(config)  @TODO: add later?
        self.config = config

    def _load_train_config(self):
        train_config_rw = TrainConfigRW(self.helper.paths.model_dir)
        if not train_config_rw.train_config_exists:
            raise RuntimeError('Unable to run inference, config from training wasn\'t found.')
        self.train_config = train_config_rw.load()

        src_size = self.train_config['settings']['input_size']
        self.input_size_wh = (src_size['width'], src_size['height'])
        logger.info('Model input size is read (for auto-rescale).', extra={'input_size': {
            'width': self.input_size_wh[0], 'height': self.input_size_wh[1]
        }})

        self.class_title_to_idx = self.train_config['class_title_to_idx']
        self.train_classes = sly.FigClasses(self.train_config['out_classes'])
        logger.info('Read model internal class mapping', extra={'class_mapping': self.class_title_to_idx})
        logger.info('Read model out classes', extra={'classes': self.train_classes.py_container})

        self.out_class_mapping = {x: self.class_title_to_idx[x] for x in
                                  (x['title'] for x in self.train_classes)}

    def _construct_and_fill_model(self):
        logger.info('Will create model.')
        with tf.get_default_graph().as_default():
            img_np = tf.placeholder(tf.float32, shape=(None, None, 3))
            img_shape = tf.shape(img_np)

            w, h = self.input_size_wh
            img_np_4d = tf.expand_dims(img_np, axis=0)
            image_rs_4d = tf.image.resize_bilinear(img_np_4d, (h, w), align_corners=True)
            image_rs = tf.squeeze(image_rs_4d, axis=0)
            img = preprocess(image_rs, h, w)

            net = ICNet_BN({'data': img},
                           is_training=False, num_classes=len(self.train_classes),
                           filter_scale=self.train_config['settings']['filter_scale'])
            raw_output = net.layers['conv6_cls']  # 4d

            # Predictions.
            # !!!!!!!!!  align_corners=False
            raw_output_up = tf.image.resize_bilinear(raw_output,
                                                     size=[img_shape[0], img_shape[1]], align_corners=False)
            # raw_output_up = tf.argmax(raw_output_up, dimension=3)

            logger.info('Will load weights from trained model.')
            # Init tf Session
            config = tf.ConfigProto()
            config.gpu_options.allow_growth = True
            sess = tf.Session(config=config)
            init = tf.global_variables_initializer()
            sess.run(init)
            loader = tf.train.Saver(var_list=tf.global_variables())

            # last_checkpoint = tf_saver.latest_checkpoint(output_train_dir)
            last_checkpoint = osp.join(self.helper.paths.model_dir, 'model.ckpt')
            loader.restore(sess, last_checkpoint)

            self.input_images = img_np
            self.predictions = raw_output_up
            self.sess = sess
        logger.info('Model has been created & weights are loaded.')

    def _determine_input_data(self):
        project_fs = sly.ProjectFS.from_disk_dir_project(self.helper.paths.project_dir)
        logger.info('Project structure has been read. Samples: {}.'.format(project_fs.pr_structure.image_cnt))
        self.in_project_fs = project_fs

        self.inf_feeder = sly.InferenceFeederFactory.create(
            self.config, self.helper.in_project_meta, self.train_classes
        )
        if self.inf_feeder.expected_result == sly.InfResultsToFeeder.FIGURES:
            self._postproc = lambda a, pred: sly.prediction_to_sly_bitmaps(a, np.argmax(pred, axis=2))
        elif self.inf_feeder.expected_result == sly.InfResultsToFeeder.SEGMENTATION:
            self._postproc = lambda a, b: (a, b)
        else:
            raise NotImplementedError()

    def __init__(self):
        logger.info('Will init all required to inference.')
        self.helper = sly.TaskHelperInference()

        self._determine_settings()
        self.device_ids = sly.remap_gpu_devices(self.config['gpu_devices'])

        self._load_train_config()
        self._construct_and_fill_model()
        self._determine_input_data()

        self.debug_copy_images = os.getenv('DEBUG_COPY_IMAGES') is not None

        logger.info('Model is ready to inference.')

    def _infer_on_img(self, img, _):
        img_var = img.astype(np.float32)
        semantic_predictions = self.sess.run(self.predictions,
                                             feed_dict={self.input_images: img_var})

        pred = np.squeeze(semantic_predictions[0])
        res = self._postproc(self.out_class_mapping, pred)
        return res

    def run_inference(self):
        out_project_fs = copy(self.in_project_fs)
        out_project_fs.root_path = self.helper.paths.results_dir
        out_project_fs.make_dirs()

        out_pr_meta = self.inf_feeder.out_meta
        out_pr_meta.to_dir(out_project_fs.project_path)

        ia_cnt = out_project_fs.pr_structure.image_cnt
        progress = sly.progress_counter_inference(cnt_imgs=ia_cnt)

        for sample in self.in_project_fs:
            logger.trace('Will process image',
                         extra={'dataset_name': sample.ds_name, 'image_name': sample.image_name})
            ann_packed = sly.json_load(sample.ann_path)
            ann = sly.Annotation.from_packed(ann_packed, self.helper.in_project_meta)

            img = cv2.imread(sample.img_path)[:, :, ::-1]
            res_ann = self.inf_feeder.feed(img, ann, self._infer_on_img)

            out_ann_fpath = out_project_fs.ann_path(sample.ds_name, sample.image_name)
            res_ann_packed = res_ann.pack()
            sly.json_dump(res_ann_packed, out_ann_fpath)

            if self.debug_copy_images:
                out_img_fpath = out_project_fs.img_path(sample.ds_name, sample.image_name)
                sly.ensure_base_path(out_img_fpath)
                shutil.copy(sample.img_path, out_img_fpath)

            progress.iter_done_report()

        sly.report_inference_finished()


def main():
    x = ICNetApplier()
    x.run_inference()


if __name__ == '__main__':
    if os.getenv('DEBUG_LOG_TO_FILE', None):
        sly.add_default_logging_into_file(logger, sly.TaskPaths().debug_dir)
    sly.main_wrapper('ICNET_INFERENCE', main)
