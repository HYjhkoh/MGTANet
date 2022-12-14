import copy
import pathlib
import pickle
import time
import os.path as osp
from functools import partial, reduce

import numpy as np
import cv2
from skimage import io

from det3d.core.bbox import box_np_ops
from det3d.core.sampler import preprocess as prep
from det3d.utils.check import shape_mergeable
from det3d.datasets.nuscenes.nusc_common import get_lidar2cam_matrix, view_points

def get_image(path):
    """
    Loads image for a sample. Copied from PCDet.
    Args:
        idx: int, Sample index
    Returns:
        image: (H, W, 3), RGB Image
    """
    assert osp.exists(path)
    image = io.imread(path)
    image = image.astype(np.float32)
    image /= 255.0
    return image
    
class DataBaseSamplerV2:
    def __init__(
        self,
        db_infos,
        groups,
        db_prepor=None,
        rate=1.0,
        global_rot_range=None,
        logger=None,
        seq_len=None,
        sequence_mode=None
    ):
        # import pdb; pdb.set_trace()
        for k, v in db_infos.items():
            logger.info(f"load {len(v)} {k} database infos")

        if db_prepor is not None:
            db_infos = db_prepor(db_infos)
            logger.info("After filter database:")
            for k, v in db_infos.items():
                logger.info(f"load {len(v)} {k} database infos")

        self.db_infos = db_infos
        self._rate = rate
        self._groups = groups
        self._group_db_infos = {}
        self._group_name_to_names = []
        self._sample_classes = []
        self._sample_max_nums = []
        self._use_group_sampling = False  # slower
        self.seq_len = seq_len
        self.sequence_mode = sequence_mode
        if any([len(g) > 1 for g in groups]):
            self._use_group_sampling = True
        if not self._use_group_sampling:
            self._group_db_infos = self.db_infos  # just use db_infos
            for group_info in groups:
                group_names = list(group_info.keys())
                self._sample_classes += group_names
                self._sample_max_nums += list(group_info.values())
        else:
            for group_info in groups:
                group_dict = {}
                group_names = list(group_info.keys())
                group_name = ", ".join(group_names)
                self._sample_classes += group_names
                self._sample_max_nums += list(group_info.values())
                self._group_name_to_names.append((group_name, group_names))
                # self._group_name_to_names[group_name] = group_names
                for name in group_names:
                    for item in db_infos[name]:
                        gid = item["group_id"]
                        if gid not in group_dict:
                            group_dict[gid] = [item]
                        else:
                            group_dict[gid] += [item]
                if group_name in self._group_db_infos:
                    raise ValueError("group must be unique")
                group_data = list(group_dict.values())
                self._group_db_infos[group_name] = group_data
                info_dict = {}
                if len(group_info) > 1:
                    for group in group_data:
                        names = [item["name"] for item in group]
                        names = sorted(names)
                        group_name = ", ".join(names)
                        if group_name in info_dict:
                            info_dict[group_name] += 1
                        else:
                            info_dict[group_name] = 1
                print(info_dict)

        self._sampler_dict = {}
        for k, v in self._group_db_infos.items():
            self._sampler_dict[k] = prep.BatchSampler(v, k)
        self._enable_global_rot = False
        if global_rot_range is not None:
            if not isinstance(global_rot_range, (list, tuple, np.ndarray)):
                global_rot_range = [-global_rot_range, global_rot_range]
            else:
                assert shape_mergeable(global_rot_range, [2])
            if np.abs(global_rot_range[0] - global_rot_range[1]) >= 1e-3:
                self._enable_global_rot = True
        self._global_rot_range = global_rot_range

    @property
    def use_group_sampling(self):
        return self._use_group_sampling

    def sample_all(
        self,
        root_path,
        gt_boxes,
        gt_names,
        num_point_features,
        random_crop=False,
        gt_group_ids=None,
        calib=None,
        road_planes=None,
        gt_token=None,
        data_info=None,
        cam_images=None,
    ):
        sampled_num_dict = {}
        sample_num_per_class = []
        for class_name, max_sample_num in zip(
            self._sample_classes, self._sample_max_nums
        ):
            sampled_num = int(
                max_sample_num - np.sum([n == class_name for n in gt_names])
            )

            sampled_num = np.round(self._rate * sampled_num).astype(np.int64)
            sampled_num_dict[class_name] = sampled_num
            sample_num_per_class.append(sampled_num)

        sampled_groups = self._sample_classes
        if self._use_group_sampling:
            assert gt_group_ids is not None
            sampled_groups = []
            sample_num_per_class = []
            for group_name, class_names in self._group_name_to_names:
                sampled_nums_group = [sampled_num_dict[n] for n in class_names]
                sampled_num = np.max(sampled_nums_group)
                sample_num_per_class.append(sampled_num)
                sampled_groups.append(group_name)
            total_group_ids = gt_group_ids
        sampled = []
        sampled_gt_boxes = []
        avoid_coll_boxes = gt_boxes

        for class_name, sampled_num in zip(sampled_groups, sample_num_per_class):
            if sampled_num > 0:
                if self._use_group_sampling:
                    sampled_cls = self.sample_group(
                        class_name, sampled_num, avoid_coll_boxes, total_group_ids
                    )
                else:
                    sampled_cls = self.sample_class_v2(
                        class_name, sampled_num, avoid_coll_boxes
                    )

                sampled += sampled_cls
                if len(sampled_cls) > 0:
                    if len(sampled_cls) == 1:
                        sampled_gt_box = sampled_cls[0]["box3d_lidar"][np.newaxis, ...]
                    else:
                        sampled_gt_box = np.stack(
                            [s["box3d_lidar"] for s in sampled_cls], axis=0
                        )

                    sampled_gt_boxes += [sampled_gt_box]
                    avoid_coll_boxes = np.concatenate(
                        [avoid_coll_boxes, sampled_gt_box], axis=0
                    )
                    if self._use_group_sampling:
                        if len(sampled_cls) == 1:
                            sampled_group_ids = np.array(sampled_cls[0]["group_id"])[
                                np.newaxis, ...
                            ]
                        else:
                            sampled_group_ids = np.stack(
                                [s["group_id"] for s in sampled_cls], axis=0
                            )
                        total_group_ids = np.concatenate(
                            [total_group_ids, sampled_group_ids], axis=0
                        )

        if len(sampled) > 0:
            sampled_gt_boxes = np.concatenate(sampled_gt_boxes, axis=0)
            sample_coords = box_np_ops.rbbox3d_to_corners(sampled_gt_boxes)
            num_sampled = len(sampled)
            s_points_list = []
            idx_points_list = []
            crop_imgs_list = []
            for _idx, info in enumerate(sampled):
                try:
                    s_points = np.fromfile(
                        str(pathlib.Path(root_path) / info["path"]), dtype=np.float32
                    ).reshape(-1, num_point_features)

                    if "rot_transform" in info:
                        rot = info["rot_transform"]
                        s_points[:, :3] = box_np_ops.rotation_points_single_angle(
                            s_points[:, :4], rot, axis=2
                        )
                    s_points[:, :3] += info["box3d_lidar"][:3]
                    idx_points = _idx * np.ones(len(s_points), dtype=np.int64)
                    s_points_list.append(s_points)
                    idx_points_list.append(idx_points)
                    # print(pathlib.Path(info["path"]).stem)
                except Exception:
                    print(str(pathlib.Path(root_path) / info["path"]))
                    continue
                
                if data_info is not None:
                    # Transform points
                    sample_record = data_info.get('sample', info['token'])
                    pointsensor_token = sample_record['data']['LIDAR_TOP']
                    crop_img = []
                    for _key in cam_images:
                        cam_key = _key.upper()
                        camera_token = sample_record['data'][cam_key]
                        cam = data_info.get('sample_data', camera_token)
                        lidar2cam, cam_intrinsic = get_lidar2cam_matrix(data_info, pointsensor_token, cam)
                        points_3d = np.concatenate([sample_coords[_idx], np.ones((len(sample_coords[_idx]), 1))], axis=-1)
                        # points_cam = points_3d @ lidar2cam.T
                        points_cam = lidar2cam @ points_3d.T
                        # Filter useless boxes according to depth
                        if not (points_cam[2,:]>0).all():
                            continue
                        point_img = view_points(points_cam[:3, :], np.array(cam_intrinsic), normalize=True)
                        point_img = point_img.transpose()[:,:2].astype(np.int64)
                        cam_img = get_image(osp.join(data_info.dataroot, cam['filename']))
                        minxy = np.min(point_img, axis=0)
                        maxxy = np.max(point_img, axis=0)
                        bbox = np.concatenate([minxy, maxxy], axis=0)
                        bbox[0::2] = np.clip(bbox[0::2], a_min=0, a_max=cam_img.shape[1]-1)
                        bbox[1::2] = np.clip(bbox[1::2], a_min=0, a_max=cam_img.shape[0]-1)
                        if (bbox[2]-bbox[0])*(bbox[3]-bbox[1])==0:
                            continue
                        crop_img = cam_img[bbox[1]:bbox[3], bbox[0]:bbox[2]]
                        break
                    
                    crop_imgs_list.append(crop_img)
                        
            if random_crop:
                s_points_list_new = []
                idx_points_list_new = []
                assert calib is not None
                rect = calib["rect"]
                Trv2c = calib["Trv2c"]
                P2 = calib["P2"]
                gt_bboxes = box_np_ops.box3d_to_bbox(sampled_gt_boxes, rect, Trv2c, P2)
                crop_frustums = prep.random_crop_frustum(gt_bboxes, rect, Trv2c, P2)
                for i in range(crop_frustums.shape[0]):
                    s_points = s_points_list[i]
                    mask = prep.mask_points_in_corners(
                        s_points, crop_frustums[i : i + 1]
                    ).reshape(-1)
                    num_remove = np.sum(mask)
                    if num_remove > 0 and (s_points.shape[0] - num_remove) > 15:
                        s_points = s_points[np.logical_not(mask)]
                    idx_points = _idx * np.ones(len(s_points), dtype=np.int64)
                    s_points_list_new.append(s_points)
                    idx_points_list_new.append(idx_points)
                s_points_list = s_points_list_new
                idx_points_list = idx_points_list_new
            ret = {
                "gt_names": np.array([s["name"] for s in sampled]),
                "difficulty": np.array([s["difficulty"] for s in sampled]),
                "gt_boxes": sampled_gt_boxes,
                "points": np.concatenate(s_points_list, axis=0),
                "gt_masks": np.ones((num_sampled,), dtype=np.bool_),
                "img_crops": crop_imgs_list,
                "points_idx": np.concatenate(idx_points_list, axis=0)
            }
            if self._use_group_sampling:
                ret["group_ids"] = np.array([s["group_id"] for s in sampled])
            else:
                ret["group_ids"] = np.arange(
                    gt_boxes.shape[0], gt_boxes.shape[0] + len(sampled)
                )
        else:
            ret = None
        return ret

    def sample_all_vid(
        self,
        root_path,
        gt_boxes_all,
        gt_names_all,
        num_point_features,
        random_crop=False,
        gt_group_ids=None,
        calib=None,
        road_planes=None,
        gt_token=None,
        data_info=None,
        cam_images=None,
    ):
        # import pdb; pdb.set_trace()
        # if self.sequence_mode == "online" or self.sequence_mode == "copy":
        #     cur_idx = 0
        # elif self.sequence_mode == "offline":
        #     cur_idx = int((self.seq_len-1)/2)
        sampled_num_dict = {}
        sample_num_per_class = []
        for class_name, max_sample_num in zip(
            self._sample_classes, self._sample_max_nums
        ):
            sampled_num = int(
                max_sample_num - np.sum([n == class_name for n in gt_names_all[0]])
            )

            sampled_num = np.round(self._rate * sampled_num).astype(np.int64)
            sampled_num_dict[class_name] = sampled_num
            sample_num_per_class.append(sampled_num)

        sampled_groups = self._sample_classes
        if self._use_group_sampling:
            assert gt_group_ids is not None
            sampled_groups = []
            sample_num_per_class = []
            for group_name, class_names in self._group_name_to_names:
                sampled_nums_group = [sampled_num_dict[n] for n in class_names]
                sampled_num = np.max(sampled_nums_group)
                sample_num_per_class.append(sampled_num)
                sampled_groups.append(group_name)
            total_group_ids = gt_group_ids
        sampled = []
        sampled_gt_boxes = []
        avoid_coll_boxes_all = gt_boxes_all

        for class_name, sampled_num in zip(sampled_groups, sample_num_per_class):
            if sampled_num > 0:
                if self._use_group_sampling:
                    sampled_cls = self.sample_group(
                        class_name, sampled_num, avoid_coll_boxes, total_group_ids
                    )
                else:
                    sampled_cls = self.sample_class_v2_vid(
                        class_name, sampled_num, avoid_coll_boxes_all
                    )

                sampled += sampled_cls
                if len(sampled_cls) > 0:
                    if len(sampled_cls) == 1:
                        sampled_gt_box = sampled_cls[0]["box3d_lidar"][np.newaxis, ...]
                    else:
                        sampled_gt_box = np.stack(
                            [s["box3d_lidar"] for s in sampled_cls], axis=0
                        )

                    sampled_gt_boxes += [sampled_gt_box]
                    avoid_coll_boxes_all = [np.concatenate(
                        [avoid_coll_boxes, sampled_gt_box], axis=0
                    ) for avoid_coll_boxes in avoid_coll_boxes_all]
                    if self._use_group_sampling:
                        if len(sampled_cls) == 1:
                            sampled_group_ids = np.array(sampled_cls[0]["group_id"])[
                                np.newaxis, ...
                            ]
                        else:
                            sampled_group_ids = np.stack(
                                [s["group_id"] for s in sampled_cls], axis=0
                            )
                        total_group_ids = np.concatenate(
                            [total_group_ids, sampled_group_ids], axis=0
                        )

        # import pdb; pdb.set_trace()
        if len(sampled) > 0:
            sampled_gt_boxes = np.concatenate(sampled_gt_boxes, axis=0)
            sample_coords = box_np_ops.rbbox3d_to_corners(sampled_gt_boxes)
            num_sampled = len(sampled)
            s_points_list = []
            idx_points_list = []
            crop_imgs_list = []
            for _idx, info in enumerate(sampled):
                try:
                    s_points = np.fromfile(
                        str(pathlib.Path(root_path) / info["path"]), dtype=np.float32
                    ).reshape(-1, num_point_features)

                    if "rot_transform" in info:
                        rot = info["rot_transform"]
                        s_points[:, :3] = box_np_ops.rotation_points_single_angle(
                            s_points[:, :4], rot, axis=2
                        )
                    s_points[:, :3] += info["box3d_lidar"][:3]
                    idx_points = _idx * np.ones(len(s_points), dtype=np.int64)
                    s_points_list.append(s_points)
                    idx_points_list.append(idx_points)
                    # print(pathlib.Path(info["path"]).stem)
                except Exception:
                    print(str(pathlib.Path(root_path) / info["path"]))
                    continue
                
                if data_info is not None:
                    # Transform points
                    sample_record = data_info.get('sample', info['token'])
                    pointsensor_token = sample_record['data']['LIDAR_TOP']
                    crop_img = []
                    for _key in cam_images:
                        cam_key = _key.upper()
                        camera_token = sample_record['data'][cam_key]
                        cam = data_info.get('sample_data', camera_token)
                        lidar2cam, cam_intrinsic = get_lidar2cam_matrix(data_info, pointsensor_token, cam)
                        points_3d = np.concatenate([sample_coords[_idx], np.ones((len(sample_coords[_idx]), 1))], axis=-1)
                        # points_cam = points_3d @ lidar2cam.T
                        points_cam = lidar2cam @ points_3d.T
                        # Filter useless boxes according to depth
                        if not (points_cam[2,:]>0).all():
                            continue
                        point_img = view_points(points_cam[:3, :], np.array(cam_intrinsic), normalize=True)
                        point_img = point_img.transpose()[:,:2].astype(np.int64)
                        cam_img = get_image(osp.join(data_info.dataroot, cam['filename']))
                        minxy = np.min(point_img, axis=0)
                        maxxy = np.max(point_img, axis=0)
                        bbox = np.concatenate([minxy, maxxy], axis=0)
                        bbox[0::2] = np.clip(bbox[0::2], a_min=0, a_max=cam_img.shape[1]-1)
                        bbox[1::2] = np.clip(bbox[1::2], a_min=0, a_max=cam_img.shape[0]-1)
                        if (bbox[2]-bbox[0])*(bbox[3]-bbox[1])==0:
                            continue
                        crop_img = cam_img[bbox[1]:bbox[3], bbox[0]:bbox[2]]
                        break
                    
                    crop_imgs_list.append(crop_img)
                        
            if random_crop:
                s_points_list_new = []
                idx_points_list_new = []
                assert calib is not None
                rect = calib["rect"]
                Trv2c = calib["Trv2c"]
                P2 = calib["P2"]
                gt_bboxes = box_np_ops.box3d_to_bbox(sampled_gt_boxes, rect, Trv2c, P2)
                crop_frustums = prep.random_crop_frustum(gt_bboxes, rect, Trv2c, P2)
                for i in range(crop_frustums.shape[0]):
                    s_points = s_points_list[i]
                    mask = prep.mask_points_in_corners(
                        s_points, crop_frustums[i : i + 1]
                    ).reshape(-1)
                    num_remove = np.sum(mask)
                    if num_remove > 0 and (s_points.shape[0] - num_remove) > 15:
                        s_points = s_points[np.logical_not(mask)]
                    idx_points = _idx * np.ones(len(s_points), dtype=np.int64)
                    s_points_list_new.append(s_points)
                    idx_points_list_new.append(idx_points)
                s_points_list = s_points_list_new
                idx_points_list = idx_points_list_new
            # import pdb; pdb.set_trace()
            ret = {
                "gt_names": np.array([s["name"] for s in sampled]),
                "difficulty": np.array([s["difficulty"] for s in sampled]),
                "gt_boxes": sampled_gt_boxes,
                "points": np.concatenate(s_points_list, axis=0),
                "gt_masks": np.ones((num_sampled,), dtype=np.bool_),
                "img_crops": crop_imgs_list,
                "points_idx": np.concatenate(idx_points_list, axis=0)
            }
            if self._use_group_sampling:
                ret["group_ids"] = np.array([s["group_id"] for s in sampled])
            else:
                ret["group_ids"] = [np.arange(
                    gt_boxes.shape[0], gt_boxes.shape[0] + len(sampled)
                ) for gt_boxes in gt_boxes_all]
        else:
            ret = None
        return ret
    
    def sample(self, name, num):
        if self._use_group_sampling:
            group_name = name
            ret = self._sampler_dict[group_name].sample(num)
            groups_num = [len(l) for l in ret]
            return reduce(lambda x, y: x + y, ret), groups_num
        else:
            ret = self._sampler_dict[name].sample(num)
            return ret, np.ones((len(ret),), dtype=np.int64)

    def sample_v1(self, name, num):
        if isinstance(name, (list, tuple)):
            group_name = ", ".join(name)
            ret = self._sampler_dict[group_name].sample(num)
            groups_num = [len(l) for l in ret]
            return reduce(lambda x, y: x + y, ret), groups_num
        else:
            ret = self._sampler_dict[name].sample(num)
            return ret, np.ones((len(ret),), dtype=np.int64)

    def sample_class_v2_vid(self, name, num, gt_boxes_all):
        # import pdb; pdb.set_trace()
        sampled = self._sampler_dict[name].sample(num)
        sampled = copy.deepcopy(sampled)
        num_sampled = len(sampled)

        valid_samples = []
        valid_samples_mask = np.zeros([len(gt_boxes_all), len(sampled)])
        for gt_idx, gt_boxes in enumerate(gt_boxes_all):
            num_gt = gt_boxes.shape[0]
            gt_boxes_bv = box_np_ops.center_to_corner_box2d(
                gt_boxes[:, 0:2], gt_boxes[:, 3:5], gt_boxes[:, -1]
            )

            sp_boxes = np.stack([i["box3d_lidar"] for i in sampled], axis=0)
            boxes = np.concatenate([gt_boxes, sp_boxes], axis=0).copy()

            sp_boxes_new = boxes[gt_boxes.shape[0] :]
            sp_boxes_bv = box_np_ops.center_to_corner_box2d(
                sp_boxes_new[:, 0:2], sp_boxes_new[:, 3:5], sp_boxes_new[:, -1]
            )

            total_bv = np.concatenate([gt_boxes_bv, sp_boxes_bv], axis=0)
            coll_mat = prep.box_collision_test(total_bv, total_bv)
            diag = np.arange(total_bv.shape[0])
            coll_mat[diag, diag] = False
            
            valid_samples_idx = []
            for i in range(num_gt, num_gt + num_sampled):
                if coll_mat[i].any():
                    coll_mat[i] = False
                    coll_mat[:, i] = False
                else:
                    valid_samples_idx.append(i - num_gt)
            valid_samples_mask[gt_idx, valid_samples_idx] = 1

        for i in range(len(valid_samples_mask)):
            if i == 0:
                sample_mask = valid_samples_mask[i]
            else:
                sample_mask *= valid_samples_mask[i]

        for i in range(len(sample_mask)):
            if sample_mask[i] == 1:
                valid_samples.append(sampled[i])
            else:
                continue
                
        # import pdb; pdb.set_trace()
        return valid_samples

    
    def sample_class_v2(self, name, num, gt_boxes):
        sampled = self._sampler_dict[name].sample(num)
        sampled = copy.deepcopy(sampled)
        num_gt = gt_boxes.shape[0]
        num_sampled = len(sampled)
        gt_boxes_bv = box_np_ops.center_to_corner_box2d(
            gt_boxes[:, 0:2], gt_boxes[:, 3:5], gt_boxes[:, -1]
        )

        sp_boxes = np.stack([i["box3d_lidar"] for i in sampled], axis=0)

        valid_mask = np.zeros([gt_boxes.shape[0]], dtype=np.bool_)
        valid_mask = np.concatenate(
            [valid_mask, np.ones([sp_boxes.shape[0]], dtype=np.bool_)], axis=0
        )
        boxes = np.concatenate([gt_boxes, sp_boxes], axis=0).copy()
        if self._enable_global_rot:
            # place samples to any place in a circle.
            prep.noise_per_object_v3_(
                boxes, None, valid_mask, 0, 0, self._global_rot_range, num_try=100
            )

        sp_boxes_new = boxes[gt_boxes.shape[0] :]
        sp_boxes_bv = box_np_ops.center_to_corner_box2d(
            sp_boxes_new[:, 0:2], sp_boxes_new[:, 3:5], sp_boxes_new[:, -1]
        )

        total_bv = np.concatenate([gt_boxes_bv, sp_boxes_bv], axis=0)
        # coll_mat = collision_test_allbox(total_bv)
        coll_mat = prep.box_collision_test(total_bv, total_bv)
        diag = np.arange(total_bv.shape[0])
        coll_mat[diag, diag] = False

        valid_samples = []
        for i in range(num_gt, num_gt + num_sampled):
            if coll_mat[i].any():
                coll_mat[i] = False
                coll_mat[:, i] = False
            else:
                if self._enable_global_rot:
                    sampled[i - num_gt]["box3d_lidar"][:2] = boxes[i, :2]
                    sampled[i - num_gt]["box3d_lidar"][-1] = boxes[i, -1]
                    sampled[i - num_gt]["rot_transform"] = (
                        boxes[i, -1] - sp_boxes[i - num_gt, -1]
                    )
                valid_samples.append(sampled[i - num_gt])
        return valid_samples

    def sample_group(self, name, num, gt_boxes, gt_group_ids):
        sampled, group_num = self.sample(name, num)
        sampled = copy.deepcopy(sampled)
        # rewrite sampled group id to avoid duplicated with gt group ids
        gid_map = {}
        max_gt_gid = np.max(gt_group_ids)
        sampled_gid = max_gt_gid + 1
        for s in sampled:
            gid = s["group_id"]
            if gid in gid_map:
                s["group_id"] = gid_map[gid]
            else:
                gid_map[gid] = sampled_gid
                s["group_id"] = sampled_gid
                sampled_gid += 1

        num_gt = gt_boxes.shape[0]
        gt_boxes_bv = box_np_ops.center_to_corner_box2d(
            gt_boxes[:, 0:2], gt_boxes[:, 3:5], gt_boxes[:, -1]
        )

        sp_boxes = np.stack([i["box3d_lidar"] for i in sampled], axis=0)
        sp_group_ids = np.stack([i["group_id"] for i in sampled], axis=0)
        valid_mask = np.zeros([gt_boxes.shape[0]], dtype=np.bool_)
        valid_mask = np.concatenate(
            [valid_mask, np.ones([sp_boxes.shape[0]], dtype=np.bool_)], axis=0
        )
        boxes = np.concatenate([gt_boxes, sp_boxes], axis=0).copy()
        group_ids = np.concatenate([gt_group_ids, sp_group_ids], axis=0)
        if self._enable_global_rot:
            # place samples to any place in a circle.
            prep.noise_per_object_v3_(
                boxes,
                None,
                valid_mask,
                0,
                0,
                self._global_rot_range,
                group_ids=group_ids,
                num_try=100,
            )
        sp_boxes_new = boxes[gt_boxes.shape[0] :]
        sp_boxes_bv = box_np_ops.center_to_corner_box2d(
            sp_boxes_new[:, 0:2], sp_boxes_new[:, 3:5], sp_boxes_new[:, -1]
        )
        total_bv = np.concatenate([gt_boxes_bv, sp_boxes_bv], axis=0)
        # coll_mat = collision_test_allbox(total_bv)
        coll_mat = prep.box_collision_test(total_bv, total_bv)
        diag = np.arange(total_bv.shape[0])
        coll_mat[diag, diag] = False
        valid_samples = []
        idx = num_gt
        for num in group_num:
            if coll_mat[idx : idx + num].any():
                coll_mat[idx : idx + num] = False
                coll_mat[:, idx : idx + num] = False
            else:
                for i in range(num):
                    if self._enable_global_rot:
                        sampled[idx - num_gt + i]["box3d_lidar"][:2] = boxes[
                            idx + i, :2
                        ]
                        sampled[idx - num_gt + i]["box3d_lidar"][-1] = boxes[
                            idx + i, -1
                        ]
                        sampled[idx - num_gt + i]["rot_transform"] = (
                            boxes[idx + i, -1] - sp_boxes[idx + i - num_gt, -1]
                        )

                    valid_samples.append(sampled[idx - num_gt + i])
            idx += num
        return valid_samples

# debug use
# image_test = (cam_img * 255).astype(np.uint8)
# image_test = np.ascontiguousarray(image_test)
# cv2.rectangle(image_test, tuple(bbox[:2]), tuple(bbox[2:]), (0,255,0), 2)
# points_3d = s_points[:,:4].copy()
# points_3d[:,-1] = 1
# points_cam = lidar2cam @ points_3d.T
# depth = points_cam[2,:]
# point_img = view_points(points_cam[:3, :], np.array(cam_intrinsic), normalize=True)
# point_img = point_img.transpose()[:,:2].astype(np.int)
# _mask = (depth > 0) & (point_img[:,0] > 0) & (point_img[:,0] < cam_img.shape[1]-1) & \
#         (point_img[:,1] > 0) & (point_img[:,1] < cam_img.shape[0]-1)
# point_img = point_img[_mask]
# for _point in point_img:
#     circle_coord = tuple(_point)
#     cv2.circle(image_test, circle_coord, 3, (0,255,0), -1)
# # debug use
# import ipdb; ipdb.set_trace()
# print(_key)

# cv2.imwrite('image_test.jpg', image_test)

class DataBaseSampler_Temporal_sampling:
    def __init__(
        self,
        db_infos_list,
        groups,
        db_prepor=None,
        rate=1.0,
        global_rot_range=None,
        logger=None,
        seq_len=None,
        sequence_mode=None
    ):
        # import pdb; pdb.set_trace()
        for t , db_infos in enumerate(db_infos_list):
            for k, v in db_infos.items():
                logger.info(f"load {len(v)} {k} database infos for temporal idx {t}")

        if db_prepor is not None:
            db_infos_list = db_prepor(db_infos_list)
            logger.info("After filter database:")
            for t , db_infos in enumerate(db_infos_list):
                for k, v in db_infos.items():
                    logger.info(f"load {len(v)} {k} database infos for temporal idx {t}")

        # self.db_infos_list <-- list of db_infos for temporal gt sampling
        self.db_infos_list = db_infos_list

        self._rate = rate
        self._groups = groups
        self._group_db_infos = {}
        self._group_name_to_names = []
        self._sample_classes = []
        self._sample_max_nums = []
        self._use_group_sampling = False  # slower
        self.seq_len = seq_len
        self.sequence_mode = sequence_mode
        if any([len(g) > 1 for g in groups]):
            self._use_group_sampling = True
        
        if not self._use_group_sampling:
            self._group_db_infos = self.db_infos_list[0]  # just use db_infos
            for group_info in groups:
                group_names = list(group_info.keys())
                self._sample_classes += group_names
                self._sample_max_nums += list(group_info.values())
        else:
            raise Exception('Not implementation error')

        self._sampler_dict = {}
        for k, v in self._group_db_infos.items():
            self._sampler_dict[k] = prep.BatchSampler(v, k)
        
        self._enable_global_rot = False
        if global_rot_range is not None:
            if not isinstance(global_rot_range, (list, tuple, np.ndarray)):
                global_rot_range = [-global_rot_range, global_rot_range]
            else:
                assert shape_mergeable(global_rot_range, [2])
            if np.abs(global_rot_range[0] - global_rot_range[1]) >= 1e-3:
                self._enable_global_rot = True
        self._global_rot_range = global_rot_range

    @property
    def use_group_sampling(self):
        return self._use_group_sampling

    def temporal_sample_all_vid(
        self,
        temporal_mode,
        root_path,
        gt_boxes_all,
        gt_names_all,
        num_point_features,
        random_crop=False,
        gt_group_ids=None,
        calib=None,
        road_planes=None,
        gt_token=None,
        data_info=None,
        cam_images=None,
    ):
        if self.sequence_mode == "online" or self.sequence_mode == "copy":
            cur_idx = 0
        elif self.sequence_mode == "offline":
            cur_idx = int((self.seq_len-1)/2)
        sampled_num_dict = {}
        sample_num_per_class = []
        for class_name, max_sample_num in zip(self._sample_classes, self._sample_max_nums):
            sampled_num = int(
                max_sample_num - np.sum([n == class_name for n in gt_names_all[cur_idx]])
            )

            sampled_num = np.round(self._rate * sampled_num).astype(np.int64)
            sampled_num_dict[class_name] = sampled_num
            sample_num_per_class.append(sampled_num)

        sampled_groups = self._sample_classes
        
        sampled                 = [[] for t in range(self.seq_len)]
        sampled_gt_boxes        = [[] for t in range(self.seq_len)]
        avoid_coll_boxes_all    = gt_boxes_all

        for class_name, sampled_num in zip(sampled_groups, sample_num_per_class):
            if sampled_num > 0:
                sampled_cls_list = self.temporal_sample_class_v2_vid( class_name, sampled_num, avoid_coll_boxes_all)
                assert type(sampled_cls_list) == list
                assert len(sampled_cls_list) == self.seq_len

                sampled_cur     = sampled_cls_list[0]
                sampled_prev_1  = sampled_cls_list[1]
                sampled_next_1  = sampled_cls_list[2]
                    
                sampled[0] += sampled_cur
                sampled[1] += sampled_prev_1
                sampled[2] += sampled_next_1

                if len(sampled_cur) > 0:
                    sampled_gt_box = [[] for t in range(self.seq_len)]

                    if len(sampled_cur) == 1:
                        sampled_gt_box[0] = sampled_cur[0]["box3d_lidar"][np.newaxis, ...]
                        sampled_gt_box[1] = sampled_prev_1[0]["box3d_lidar"][np.newaxis, ...]
                        sampled_gt_box[2] = sampled_next_1[0]["box3d_lidar"][np.newaxis, ...]
                    else:
                        sampled_gt_box[0] = np.stack([s["box3d_lidar"] for s in sampled_cur], axis=0)
                        sampled_gt_box[1] = np.stack([s["box3d_lidar"] for s in sampled_prev_1], axis=0)
                        sampled_gt_box[2] = np.stack([s["box3d_lidar"] for s in sampled_next_1], axis=0)



                if len(sampled_cur) > 0:
                    for t in range(self.seq_len):
                        sampled_gt_boxes[t] += [sampled_gt_box[t]]

                    avoid_coll_boxes_all = [np.concatenate([avoid_coll_boxes, sampled_gt_box[t]], axis=0) for t, avoid_coll_boxes in enumerate(avoid_coll_boxes_all)]

        if len(sampled_cur) > 0:
            sampled_gt_boxes_list = []
            num_sampled_list = []
            s_points_list = []
            idx_points_list = []
            gt_names_list = []
            difficulty_list = []

            for t in range(self.seq_len):
                sampled_gt_boxes_list.append(np.concatenate(sampled_gt_boxes[t], axis=0))

                num_sampled = len(sampled[t])
                num_sampled_list.append(np.ones((num_sampled,), dtype=np.bool_))

                s_points_temp_list =[]
                idx_points_temp_list =[]
                for _idx, info in enumerate(sampled[t]):
                    try:
                        s_points = np.fromfile(
                            str(pathlib.Path(root_path) / info["path"]), dtype=np.float32
                        ).reshape(-1, num_point_features)

                        s_points[:, :3] += info["box3d_lidar"][:3]
                        idx_points = _idx * np.ones(len(s_points), dtype=np.int64)
                        s_points_temp_list.append(s_points)
                        idx_points_temp_list.append(idx_points)
                    except Exception:
                        print(str(pathlib.Path(root_path) / info["path"]))
                        continue

                s_points_list.append(np.concatenate(s_points_temp_list, axis=0))
                idx_points_list.append(np.concatenate(idx_points_temp_list, axis=0))
                gt_names_list.append(np.array([s["name"] for s in sampled[t]]))
                difficulty_list.append(np.array([s["difficulty"] for s in sampled[t]]))
            
            crop_imgs_list = []
            
            ret = {
                "gt_names": gt_names_list,
                "difficulty": difficulty_list,
                "gt_boxes": sampled_gt_boxes_list,
                "points": s_points_list,
                "gt_masks": num_sampled_list,
                "img_crops": crop_imgs_list,
                "points_idx": idx_points_list
            }
            
            ret["group_ids"] = [np.arange(
                    gt_boxes.shape[0], gt_boxes.shape[0] + len(sampled)
                ) for gt_boxes in gt_boxes_all]
        else:
            ret = None
        return ret

    def sample(self, name, num):
        if self._use_group_sampling:
            group_name = name
            ret = self._sampler_dict[group_name].sample(num)
            groups_num = [len(l) for l in ret]
            return reduce(lambda x, y: x + y, ret), groups_num
        else:
            ret = self._sampler_dict[name].sample(num)
            return ret, np.ones((len(ret),), dtype=np.int64)


    def temporal_sample_class_v2_vid(self, name, num, gt_boxes_all):
        sampled_list = []
        
        sampled, sample_idx = self._sampler_dict[name].temporal_sample(num)
        sampled = copy.deepcopy(sampled)
        num_sampled = len(sampled)
        sampled_list.append(sampled)

        # Temporal sampling according to sampling_idx form batch sampler function ###
        for t in range(self.seq_len):
            if t == 0:
                continue # Exclude current target frame
            temp_sampled = [self.db_infos_list[t][name][i] for i in sample_idx]
            sampled_list.append(copy.deepcopy(temp_sampled))
        #############################################################################

        valid_samples_list = [[] for _ in range(self.seq_len)]
        valid_samples_mask = np.zeros([len(gt_boxes_all), len(sampled)])

        for gt_idx, gt_boxes in enumerate(gt_boxes_all):
            num_gt = gt_boxes.shape[0]
            gt_boxes_bv = box_np_ops.center_to_corner_box2d(
                gt_boxes[:, 0:2], gt_boxes[:, 3:5], gt_boxes[:, -1]
            )

            sp_boxes = np.stack([i["box3d_lidar"] for i in sampled_list[gt_idx]], axis=0)
            boxes = np.concatenate([gt_boxes, sp_boxes], axis=0).copy()

            sp_boxes_new = boxes[gt_boxes.shape[0] :]
            sp_boxes_bv = box_np_ops.center_to_corner_box2d(sp_boxes_new[:, 0:2], sp_boxes_new[:, 3:5], sp_boxes_new[:, -1]
                )
            total_bv = np.concatenate([gt_boxes_bv, sp_boxes_bv], axis=0)
            coll_mat = prep.box_collision_test(total_bv, total_bv)
            diag = np.arange(total_bv.shape[0])
            coll_mat[diag, diag] = False
                
            valid_samples_idx = []

            for i in range(num_gt, num_gt + num_sampled):
                if coll_mat[i].any():
                    coll_mat[i] = False
                    coll_mat[:, i] = False
                else:
                    valid_samples_idx.append(i - num_gt)
            valid_samples_mask[gt_idx, valid_samples_idx] = 1

        for i in range(len(valid_samples_mask)):
            if i == 0:
                sample_mask = valid_samples_mask[i]
            else:
                sample_mask *= valid_samples_mask[i]

        for i in range(len(sample_mask)):
            if sample_mask[i] == 1:
                for t in range(self.seq_len):
                    valid_samples_list[t].append(sampled_list[t][i])
            else:
                continue
                
        return valid_samples_list