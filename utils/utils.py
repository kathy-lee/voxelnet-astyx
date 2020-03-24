import cv2
import numpy as np
import math
import json

#from config import cfg
from utils.box_overlaps import *
from config import cfg

def process_pointcloud(point_cloud, cfg):
    # Input:
    #   (N, 4)
    # Output:
    #   voxel_dict
    scene_size = np.array(cfg.SCENE_SIZE, dtype=np.float32)
    voxel_size = np.array(cfg.VOXEL_SIZE, dtype=np.float32)
    grid_size = np.array(cfg.GRID_SIZE, dtype=np.int64)
    lidar_coord = np.array(cfg.LIDAR_COORD, dtype=np.float32)
    max_point_number = cfg.MAX_POINT_NUMBER
    
    if cfg.DETECT_OBJECT != "Car":
        np.random.shuffle(point_cloud)

    shifted_coord = point_cloud[:, :3] + lidar_coord
    # reverse the point cloud coordinate (X, Y, Z) -> (Z, Y, X)
    voxel_index = np.floor(
        shifted_coord[:, ::-1] / voxel_size).astype(np.int)

    bound_x = np.logical_and(
        voxel_index[:, 2] >= 0, voxel_index[:, 2] < grid_size[2])
    bound_y = np.logical_and(
        voxel_index[:, 1] >= 0, voxel_index[:, 1] < grid_size[1])
    bound_z = np.logical_and(
        voxel_index[:, 0] >= 0, voxel_index[:, 0] < grid_size[0])

    bound_box = np.logical_and(np.logical_and(bound_x, bound_y), bound_z)

    point_cloud = point_cloud[bound_box]
    voxel_index = voxel_index[bound_box]

    # [K, 3] coordinate buffer as described in the paper
    coordinate_buffer = np.unique(voxel_index, axis=0)

    K = len(coordinate_buffer)
    T = max_point_number

    # [K, 1] store number of points in each voxel grid
    number_buffer = np.zeros(shape=(K), dtype=np.int64)

    # [K, T, 7] feature buffer as described in the paper
    feature_buffer = np.zeros(shape=(K, T, 7), dtype=np.float32)

    # build a reverse index for coordinate buffer
    index_buffer = {}
    for i in range(K):
        index_buffer[tuple(coordinate_buffer[i])] = i

    for voxel, point in zip(voxel_index, point_cloud):
        index = index_buffer[tuple(voxel)]
        number = number_buffer[index]
        if number < T:
            feature_buffer[index, number, :4] = point
            number_buffer[index] += 1

    feature_buffer[:, :, -3:] = feature_buffer[:, :, :3] - \
        feature_buffer[:, :, :3].sum(axis=1, keepdims=True)/number_buffer.reshape(K, 1, 1)

    voxel_dict = {'feature_buffer': feature_buffer,
                  'coordinate_buffer': coordinate_buffer,
                  'number_buffer': number_buffer}
    return voxel_dict

# transformation matrix converts from sensorA->sensorB to sensorB->sensorA
def inv_trans(T):
    rotation = np.linalg.inv(T[0:3, 0:3])  # rotation matrix

    translation = T[0:3, 3]
    translation = -1 * np.dot(rotation, translation.T)
    translation = np.reshape(translation, (3, 1))
    Q = np.hstack((rotation, translation))

    return Q

def quat_to_rotation(quat):
    m = np.sum(np.multiply(quat, quat))
    q = quat.copy()
    q = np.array(q)
    n = np.dot(q, q)
    if n < np.finfo(q.dtype).eps:
        rot_matrix = np.identity(4)
        return rot_matrix
    q = q * np.sqrt(2.0 / n)
    q = np.outer(q, q)
    rot_matrix = np.array(
        [[1.0 - q[2, 2] - q[3, 3], q[1, 2] + q[3, 0], q[1, 3] - q[2, 0]],
         [q[1, 2] - q[3, 0], 1.0 - q[1, 1] - q[3, 3], q[2, 3] + q[1, 0]],
         [q[1, 3] + q[2, 0], q[2, 3] - q[1, 0], 1.0 - q[1, 1] - q[2, 2]]],
        dtype=q.dtype)
    rot_matrix = np.transpose(rot_matrix)
    # # test if it is truly a rotation matrix
    # d = np.linalg.det(rotation)
    # t = np.transpose(rotation)
    # o = np.dot(rotation, t)
    return rot_matrix

#-- util function to load calib matrices
def load_calib(calib_dir):
    # output: 3 matrix
    with open(calib_dir, mode='r') as f:
        data = json.load(f)
    T_fromLidar = np.array(data['sensors'][1]['calib_data']['T_to_ref_COS'])
    T_fromCamera = np.array(data['sensors'][2]['calib_data']['T_to_ref_COS'])
    K = np.array(data['sensors'][2]['calib_data']['K'])

    T_toLidar = inv_trans(T_fromLidar)
    T_toCamera = inv_trans(T_fromCamera)
    return T_toLidar, T_toCamera, K

def get_class_id(classname):

    classes = {'Bus': 0, 'Car':1, 'Cyclist': 2, 'Motorcyclist': 3, 'Person': 4, 'Trailer':5, 'Truck':6}
    return classes[classname]

def load_label(label_dir):
    # output: [N,11]
    with open(label_dir, mode='r') as f:
        data = json.load(f)
    objects_info = data['objects']
    label = np.empty((len(objects_info), 11))

    for i, p in enumerate(objects_info):
        label[i,:] = np.array([p['center3d'][0], p['center3d'][1], p['center3d'][2],
                              p['dimension3d'][2], p['dimension3d'][0], p['dimension3d'][1],
                              p['orientation_quat'][0], p['orientation_quat'][1], p['orientation_quat'][2], p['orientation_quat'][3],
                              get_class_id(p['classname'])])

    return label

def lidar_to_bird_view(x, y, factor=1):
    # using the cfg.INPUT_XXX
    a = (x - cfg.X_MIN) / cfg.VOXEL_X_SIZE * factor
    b = (y - cfg.Y_MIN) / cfg.VOXEL_Y_SIZE * factor
    a = np.clip(a, a_max=(cfg.X_MAX - cfg.X_MIN) / cfg.VOXEL_X_SIZE * factor, a_min=0)
    b = np.clip(b, a_max=(cfg.Y_MAX - cfg.Y_MIN) / cfg.VOXEL_Y_SIZE * factor, a_min=0)
    return a, b

def batch_lidar_to_bird_view(points, factor=1):
    # Input:
    #   points (N, 2)
    # Outputs:
    #   points (N, 2)
    # using the cfg.INPUT_XXX
    a = (points[:, 0] - cfg.X_MIN) / cfg.VOXEL_X_SIZE * factor
    b = (points[:, 1] - cfg.Y_MIN) / cfg.VOXEL_Y_SIZE * factor
    a = np.clip(a, a_max=(cfg.X_MAX - cfg.X_MIN) / cfg.VOXEL_X_SIZE * factor, a_min=0)
    b = np.clip(b, a_max=(cfg.Y_MAX - cfg.Y_MIN) / cfg.VOXEL_Y_SIZE * factor, a_min=0)
    return np.concatenate([a[:, np.newaxis], b[:, np.newaxis]], axis=-1)


def angle_in_limit(angle):
    # To limit the angle in -pi/2 - pi/2
    limit_degree = 5
    while angle >= np.pi / 2:
        angle -= np.pi
    while angle < -np.pi / 2:
        angle += np.pi
    if abs(angle + np.pi / 2) < limit_degree / 180 * np.pi:
        angle = np.pi / 2
    return angle


def camera_to_lidar(x, y, z, T_VELO_2_CAM=None, R_RECT_0=None):
    if type(T_VELO_2_CAM) == type(None):
        T_VELO_2_CAM = np.array(cfg.MATRIX_T_VELO_2_CAM)
    
    if type(R_RECT_0) == type(None):
        R_RECT_0 = np.array(cfg.MATRIX_R_RECT_0)

    p = np.array([x, y, z, 1])
    p = np.matmul(np.linalg.inv(R_RECT_0), p)
    p = np.matmul(np.linalg.inv(T_VELO_2_CAM), p)
    p = p[0:3]
    return tuple(p)


def lidar_to_camera(x, y, z, T_VELO_2_CAM=None, R_RECT_0=None):
    if type(T_VELO_2_CAM) == type(None):
        T_VELO_2_CAM = np.array(cfg.MATRIX_T_VELO_2_CAM)
    
    if type(R_RECT_0) == type(None):
        R_RECT_0 = np.array(cfg.MATRIX_R_RECT_0)

    p = np.array([x, y, z, 1])
    p = np.matmul(T_VELO_2_CAM, p)
    p = np.matmul(R_RECT_0, p)
    p = p[0:3]
    return tuple(p)


def camera_to_lidar_point(points, T_VELO_2_CAM=None, R_RECT_0=None):
    # (N, 3) -> (N, 3)
    N = points.shape[0]
    points = np.hstack([points, np.ones((N, 1))]).T  # (N,4) -> (4,N)

    if type(T_VELO_2_CAM) == type(None):
        T_VELO_2_CAM = np.array(cfg.MATRIX_T_VELO_2_CAM)
    
    if type(R_RECT_0) == type(None):
        R_RECT_0 = np.array(cfg.MATRIX_R_RECT_0)

    points = np.matmul(np.linalg.inv(R_RECT_0), points)
    points = np.matmul(np.linalg.inv(T_VELO_2_CAM), points).T  # (4, N) -> (N, 4)
    points = points[:, 0:3]
    return points.reshape(-1, 3)


def lidar_to_camera_point(points, T_VELO_2_CAM=None):
    # (N, 3) -> (N, 3)
    N = points.shape[0]
    points = np.hstack([points, np.ones((N, 1))]).T

    points = np.matmul(T_VELO_2_CAM, points)
    points = points[:, 0:3]
    return points.reshape(-1, 3)


def camera_to_lidar_box(boxes, T_VELO_2_CAM=None, R_RECT_0=None):
    # (N, 7) -> (N, 7) x,y,z,h,w,l,r
    ret = []
    for box in boxes:
        x, y, z, h, w, l, ry = box
        (x, y, z), h, w, l, rz = camera_to_lidar(
            x, y, z, T_VELO_2_CAM, R_RECT_0), h, w, l, -ry - np.pi / 2
        rz = angle_in_limit(rz)
        ret.append([x, y, z, h, w, l, rz])
    return np.array(ret).reshape(-1, 7)


def lidar_to_camera_box(boxes, T_VELO_2_CAM=None, R_RECT_0=None):
    # (N, 7) -> (N, 7) x,y,z,h,w,l,r
    ret = []
    for box in boxes:
        x, y, z, h, w, l, rz = box
        (x, y, z), h, w, l, ry = lidar_to_camera(
            x, y, z, T_VELO_2_CAM, R_RECT_0), h, w, l, -rz - np.pi / 2
        ry = angle_in_limit(ry)
        ret.append([x, y, z, h, w, l, ry])
    return np.array(ret).reshape(-1, 7)


def center_to_corner_box2d(boxes_center, coordinate='lidar', T_VELO_2_CAM=None, R_RECT_0=None):
    # (N, 5) -> (N, 4, 2)
    N = boxes_center.shape[0]
    boxes3d_center = np.zeros((N, 7))
    boxes3d_center[:, [0, 1, 4, 5, 6]] = boxes_center
    boxes3d_corner = center_to_corner_box3d(
        boxes3d_center, coordinate=coordinate, T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)

    return boxes3d_corner[:, 0:4, 0:2]


def center_to_corner_box3d(boxes_center, coordinate='lidar', T_VELO_2_CAM=None, R_RECT_0=None):
    # (N, 10) -> (N, 8, 3)

    N = boxes_center.shape[0]
    ret = np.zeros((N, 8, 3), dtype=np.float32)

    if coordinate == 'camera':
        boxes_center = camera_to_lidar_box(boxes_center, T_VELO_2_CAM, R_RECT_0)

    for i in range(N):
        box = boxes_center[i]
        translation = box[0:3]
        size = box[3:6]
        quaternion = box[6:-1]

        w, l, h = size[0], size[1], size[2]
        trackletBox = np.array([
            [w / 2, -w / 2, -w / 2, w / 2, w / 2, -w / 2, -w / 2, w / 2],\
            [l / 2, l / 2, -l / 2, -l / 2, l / 2, l / 2, -l / 2, -l / 2],\
            [h / 2, h / 2, h / 2, h / 2, -h / 2, -h / 2, -h / 2, -h / 2]])
        # rotate and translate 3d bounding box
        R = quat_to_rotation(quaternion)
        #bbox = np.dot(R, bbox)
        #bbox = bbox + center[:, np.newaxis]

        cornerPosInVelo = np.dot(R, trackletBox) + \
            np.tile(translation, (8, 1)).T
        box3d = cornerPosInVelo.transpose()
        ret[i] = box3d

    # for idx in range(len(ret)):
    #     ret[idx] = lidar_to_camera_point(ret[idx], T_VELO_2_CAM)

    return ret


def corner_to_center_box2d(boxes_corner, coordinate='lidar', T_VELO_2_CAM=None, R_RECT_0=None):
    # (N, 4, 2) -> (N, 5)  x,y,w,l,r
    N = boxes_corner.shape[0]
    boxes3d_corner = np.zeros((N, 8, 3))
    boxes3d_corner[:, 0:4, 0:2] = boxes_corner
    boxes3d_corner[:, 4:8, 0:2] = boxes_corner
    boxes3d_center = corner_to_center_box3d(
        boxes3d_corner, coordinate=coordinate, T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)

    return boxes3d_center[:, [0, 1, 4, 5, 6]]


def corner_to_standup_box2d(boxes_corner):
    # (N, 4, 2) -> (N, 4) x1, y1, x2, y2
    N = boxes_corner.shape[0]
    standup_boxes2d = np.zeros((N, 4))
    standup_boxes2d[:, 0] = np.min(boxes_corner[:, :, 0], axis=1)
    standup_boxes2d[:, 1] = np.min(boxes_corner[:, :, 1], axis=1)
    standup_boxes2d[:, 2] = np.max(boxes_corner[:, :, 0], axis=1)
    standup_boxes2d[:, 3] = np.max(boxes_corner[:, :, 1], axis=1)

    return standup_boxes2d


# TODO: 0/90 may be not correct
def anchor_to_standup_box2d(anchors):
    # (N, 4) -> (N, 4) x,y,w,l -> x1,y1,x2,y2
    anchor_standup = np.zeros_like(anchors)
    # r == 0
    anchor_standup[::2, 0] = anchors[::2, 0] - anchors[::2, 3] / 2
    anchor_standup[::2, 1] = anchors[::2, 1] - anchors[::2, 2] / 2
    anchor_standup[::2, 2] = anchors[::2, 0] + anchors[::2, 3] / 2
    anchor_standup[::2, 3] = anchors[::2, 1] + anchors[::2, 2] / 2
    # r == pi/2
    anchor_standup[1::2, 0] = anchors[1::2, 0] - anchors[1::2, 2] / 2
    anchor_standup[1::2, 1] = anchors[1::2, 1] - anchors[1::2, 3] / 2
    anchor_standup[1::2, 2] = anchors[1::2, 0] + anchors[1::2, 2] / 2
    anchor_standup[1::2, 3] = anchors[1::2, 1] + anchors[1::2, 3] / 2

    return anchor_standup


def corner_to_center_box3d(boxes_corner, coordinate='camera', T_VELO_2_CAM=None, R_RECT_0=None):
    # (N, 8, 3) -> (N, 10) x,y,z,h,w,l,ry/z

    # if coordinate == 'lidar':
    #     for idx in range(len(boxes_corner)):
    #         boxes_corner[idx] = lidar_to_camera_point(boxes_corner[idx], T_VELO_2_CAM, R_RECT_0)
    ret = []
    for roi in boxes_corner:
        if cfg.CORNER2CENTER_AVG:  # average version
            roi = np.array(roi)
            h = abs(np.sum(roi[:4, 1] - roi[4:, 1]) / 4)
            w = np.sum(
                np.sqrt(np.sum((roi[0, [0, 2]] - roi[3, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[1, [0, 2]] - roi[2, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[4, [0, 2]] - roi[7, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[5, [0, 2]] - roi[6, [0, 2]])**2))
            ) / 4
            l = np.sum(
                np.sqrt(np.sum((roi[0, [0, 2]] - roi[1, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[2, [0, 2]] - roi[3, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[4, [0, 2]] - roi[5, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[6, [0, 2]] - roi[7, [0, 2]])**2))
            ) / 4
            x = np.sum(roi[:, 0], axis=0)/ 8
            y = np.sum(roi[0:4, 1], axis=0)/ 4
            z = np.sum(roi[:, 2], axis=0)/ 8
            ry = np.sum(
                math.atan2(roi[2, 0] - roi[1, 0], roi[2, 2] - roi[1, 2]) +
                math.atan2(roi[6, 0] - roi[5, 0], roi[6, 2] - roi[5, 2]) +
                math.atan2(roi[3, 0] - roi[0, 0], roi[3, 2] - roi[0, 2]) +
                math.atan2(roi[7, 0] - roi[4, 0], roi[7, 2] - roi[4, 2]) +
                math.atan2(roi[0, 2] - roi[1, 2], roi[1, 0] - roi[0, 0]) +
                math.atan2(roi[4, 2] - roi[5, 2], roi[5, 0] - roi[4, 0]) +
                math.atan2(roi[3, 2] - roi[2, 2], roi[2, 0] - roi[3, 0]) +
                math.atan2(roi[7, 2] - roi[6, 2], roi[6, 0] - roi[7, 0])
            ) / 8
            if w > l:
                w, l = l, w
                ry = angle_in_limit(ry + np.pi / 2)
        else:  # max version
            h = max(abs(roi[:4, 1] - roi[4:, 1]))
            w = np.max(
                np.sqrt(np.sum((roi[0, [0, 2]] - roi[3, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[1, [0, 2]] - roi[2, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[4, [0, 2]] - roi[7, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[5, [0, 2]] - roi[6, [0, 2]])**2))
            )
            l = np.max(
                np.sqrt(np.sum((roi[0, [0, 2]] - roi[1, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[2, [0, 2]] - roi[3, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[4, [0, 2]] - roi[5, [0, 2]])**2)) +
                np.sqrt(np.sum((roi[6, [0, 2]] - roi[7, [0, 2]])**2))
            )
            x = np.sum(roi[:, 0], axis=0)/ 8
            y = np.sum(roi[0:4, 1], axis=0)/ 4
            z = np.sum(roi[:, 2], axis=0)/ 8
            ry = np.sum(
                math.atan2(roi[2, 0] - roi[1, 0], roi[2, 2] - roi[1, 2]) +
                math.atan2(roi[6, 0] - roi[5, 0], roi[6, 2] - roi[5, 2]) +
                math.atan2(roi[3, 0] - roi[0, 0], roi[3, 2] - roi[0, 2]) +
                math.atan2(roi[7, 0] - roi[4, 0], roi[7, 2] - roi[4, 2]) +
                math.atan2(roi[0, 2] - roi[1, 2], roi[1, 0] - roi[0, 0]) +
                math.atan2(roi[4, 2] - roi[5, 2], roi[5, 0] - roi[4, 0]) +
                math.atan2(roi[3, 2] - roi[2, 2], roi[2, 0] - roi[3, 0]) +
                math.atan2(roi[7, 2] - roi[6, 2], roi[6, 0] - roi[7, 0])
            ) / 8
            if w > l:
                w, l = l, w
                ry = angle_in_limit(ry + np.pi / 2)
        ret.append([x, y, z, h, w, l, ry])
    if coordinate == 'lidar':
        ret = camera_to_lidar_box(np.array(ret), T_VELO_2_CAM, R_RECT_0)

    return np.array(ret)


# this just for visulize and testing
def lidar_box3d_to_camera_box(boxes3d, cal_projection=False, P2 = None, T_VELO_2_CAM=None):
    # (N, 10) -> (N, 4)/(N, 8, 2)  x,y,z,h,w,l,q0-q3 -> x1,y1,x2,y2/8*(x, y)
    num = len(boxes3d)
    boxes2d = np.zeros((num, 4), dtype=np.int32)
    projections = np.zeros((num, 8, 2), dtype=np.float32)

    lidar_boxes3d_corner = center_to_corner_box3d(boxes3d, coordinate='lidar', T_VELO_2_CAM=T_VELO_2_CAM)

    for n in range(num):
        box3d = lidar_boxes3d_corner[n]
        box3d = lidar_to_camera_point(box3d, T_VELO_2_CAM)
        points = np.hstack((box3d, np.ones((8, 1)))).T  # (8, 4) -> (4, 8)
        points = np.matmul(P2, points).T
        points[:, 0] /= points[:, 2]
        points[:, 1] /= points[:, 2]

        projections[n] = points[:, 0:2]
        minx = int(np.min(points[:, 0]))
        maxx = int(np.max(points[:, 0]))
        miny = int(np.min(points[:, 1]))
        maxy = int(np.max(points[:, 1]))

        boxes2d[n, :] = minx, miny, maxx, maxy

    return projections if cal_projection else boxes2d


def lidar_to_bird_view_img(lidar, factor=1):
    # Input:
    #   lidar: (N', 4)
    # Output:
    #   birdview: (w, l, 3)
    birdview = np.zeros(
        (cfg.INPUT_HEIGHT * factor, cfg.INPUT_WIDTH * factor, 1))
    for point in lidar:
        x, y = point[0:2]
        if cfg.X_MIN < x < cfg.X_MAX and cfg.Y_MIN < y < cfg.Y_MAX:
            x, y = int((x - cfg.X_MIN) / cfg.VOXEL_X_SIZE *
                       factor), int((y - cfg.Y_MIN) / cfg.VOXEL_Y_SIZE * factor)
            birdview[y, x] += 1
    birdview = birdview - np.min(birdview)
    divisor = np.max(birdview) - np.min(birdview)
    # TODO: adjust this factor
    birdview = np.clip((birdview / divisor * 255) *
                       5 * factor, a_min=0, a_max=255)
    birdview = np.tile(birdview, 3).astype(np.uint8)

    return birdview


def draw_lidar_box3d_on_image(img, boxes3d, scores, gt_boxes3d=np.array([]),
                              color=(0, 255, 255), gt_color=(255, 0, 255), thickness=1, P2 = None, T_VELO_2_CAM=None):
    # Input:
    #   img: (h, w, 3)
    #   boxes3d (N, 7) [x, y, z, h, w, l, r]
    #   scores
    #   gt_boxes3d (N, 7) [x, y, z, h, w, l, r]
    img = img.copy()
    projections = lidar_box3d_to_camera_box(boxes3d, cal_projection=True, P2=P2, T_VELO_2_CAM=T_VELO_2_CAM)
    gt_projections = lidar_box3d_to_camera_box(gt_boxes3d, cal_projection=True, P2=P2, T_VELO_2_CAM=T_VELO_2_CAM)

    # draw projections
    for qs in projections:
        for k in range(0, 4):
            i, j = k, (k + 1) % 4
            cv2.line(img, (qs[i, 0], qs[i, 1]), (qs[j, 0],
                                                 qs[j, 1]), color, thickness, cv2.LINE_AA)

            i, j = k + 4, (k + 1) % 4 + 4
            cv2.line(img, (qs[i, 0], qs[i, 1]), (qs[j, 0],
                                                 qs[j, 1]), color, thickness, cv2.LINE_AA)

            i, j = k, k + 4
            cv2.line(img, (qs[i, 0], qs[i, 1]), (qs[j, 0],
                                                 qs[j, 1]), color, thickness, cv2.LINE_AA)
    # draw gt projections
    for qs in gt_projections:
        for k in range(0, 4):
            i, j = k, (k + 1) % 4
            cv2.line(img, (qs[i, 0], qs[i, 1]), (qs[j, 0],
                                                 qs[j, 1]), gt_color, thickness, cv2.LINE_AA)

            i, j = k + 4, (k + 1) % 4 + 4
            cv2.line(img, (qs[i, 0], qs[i, 1]), (qs[j, 0],
                                                 qs[j, 1]), gt_color, thickness, cv2.LINE_AA)

            i, j = k, k + 4
            cv2.line(img, (qs[i, 0], qs[i, 1]), (qs[j, 0],
                                                 qs[j, 1]), gt_color, thickness, cv2.LINE_AA)

    return cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_BGR2RGB)
    


def draw_lidar_box3d_on_birdview(birdview, boxes3d, scores, gt_boxes3d=np.array([]),
                                 color=(0, 255, 255), gt_color=(255, 0, 255), thickness=1, factor=1, P2 = None, T_VELO_2_CAM=None, R_RECT_0=None):
    # Input:
    #   birdview: (h, w, 3)
    #   boxes3d (N, 7) [x, y, z, h, w, l, r]
    #   scores
    #   gt_boxes3d (N, 7) [x, y, z, h, w, l, r]
    img = birdview.copy()
    corner_boxes3d = center_to_corner_box3d(boxes3d, coordinate='lidar', T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)
    corner_gt_boxes3d = center_to_corner_box3d(gt_boxes3d, coordinate='lidar', T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)
    # draw gt
    for box in corner_gt_boxes3d:
        x0, y0 = lidar_to_bird_view(*box[0, 0:2], factor=factor)
        x1, y1 = lidar_to_bird_view(*box[1, 0:2], factor=factor)
        x2, y2 = lidar_to_bird_view(*box[2, 0:2], factor=factor)
        x3, y3 = lidar_to_bird_view(*box[3, 0:2], factor=factor)

        cv2.line(img, (int(x0), int(y0)), (int(x1), int(y1)),
                 gt_color, thickness, cv2.LINE_AA)
        cv2.line(img, (int(x1), int(y1)), (int(x2), int(y2)),
                 gt_color, thickness, cv2.LINE_AA)
        cv2.line(img, (int(x2), int(y2)), (int(x3), int(y3)),
                 gt_color, thickness, cv2.LINE_AA)
        cv2.line(img, (int(x3), int(y3)), (int(x0), int(y0)),
                 gt_color, thickness, cv2.LINE_AA)

    # draw detections
    for box in corner_boxes3d:
        x0, y0 = lidar_to_bird_view(*box[0, 0:2], factor=factor)
        x1, y1 = lidar_to_bird_view(*box[1, 0:2], factor=factor)
        x2, y2 = lidar_to_bird_view(*box[2, 0:2], factor=factor)
        x3, y3 = lidar_to_bird_view(*box[3, 0:2], factor=factor)

        cv2.line(img, (int(x0), int(y0)), (int(x1), int(y1)),
                 color, thickness, cv2.LINE_AA)
        cv2.line(img, (int(x1), int(y1)), (int(x2), int(y2)),
                 color, thickness, cv2.LINE_AA)
        cv2.line(img, (int(x2), int(y2)), (int(x3), int(y3)),
                 color, thickness, cv2.LINE_AA)
        cv2.line(img, (int(x3), int(y3)), (int(x0), int(y0)),
                 color, thickness, cv2.LINE_AA)

    return cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_BGR2RGB)


def label_to_gt_box3d(labels, cls='Car'):
    # Input:
    #   label: (N, N',10)
    #   cls: 'Car' or 'Pedestrain' or 'Cyclist'
    #   coordinate: 'camera' or 'lidar'
    # Output:
    #   (N, N', 10)
    print(f'raw labels:{labels}')
    boxes3d = []
    if cls == 'Car':
        acc_cls = ['Car', 'Van']
    elif cls == 'Pedestrian':
        acc_cls = ['Pedestrian']
    elif cls == 'Cyclist':
        acc_cls = ['Cyclist']
    else: # all
        acc_cls = []

    for label in labels:
        boxes3d_a_label = []
        for row in label:
            if row[10] in acc_cls or acc_cls == []:
                box3d = row[:-2]
                boxes3d_a_label.append(box3d)

        boxes3d.append(np.array(boxes3d_a_label).reshape(-1, 10))

    print(f'through label to gt box3d:{boxes3d}')
    return boxes3d


def box3d_to_label(batch_box3d, batch_cls, batch_score=[], coordinate='camera', P2 = None, T_VELO_2_CAM=None, R_RECT_0=None):
    # Input:
    #   (N, N', 7) x y z h w l r
    #   (N, N')
    #   cls: (N, N') 'Car' or 'Pedestrain' or 'Cyclist'
    #   coordinate(input): 'camera' or 'lidar'
    # Output:
    #   label: (N, N') N batches and N lines
    batch_label = []
    if batch_score:
        template = '{} ' + ' '.join(['{:.4f}' for i in range(15)]) + '\n'
        for boxes, scores, clses in zip(batch_box3d, batch_score, batch_cls):
            label = []
            for box, score, cls in zip(boxes, scores, clses):
                if coordinate == 'camera':
                    box3d = box
                    box2d = lidar_box3d_to_camera_box(
                        camera_to_lidar_box(box[np.newaxis, :].astype(np.float32), T_VELO_2_CAM, R_RECT_0), cal_projection=False, P2=P2, T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)[0]
                else:
                    box3d = lidar_to_camera_box(
                        box[np.newaxis, :].astype(np.float32), T_VELO_2_CAM, R_RECT_0)[0]
                    box2d = lidar_box3d_to_camera_box(
                        box[np.newaxis, :].astype(np.float32), cal_projection=False, P2=P2, T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)[0]
                x, y, z, h, w, l, r = box3d
                box3d = [h, w, l, x, y, z, r]
                label.append(template.format(
                    cls, 0, 0, 0, *box2d, *box3d, float(score)))
            batch_label.append(label)
    else:
        template = '{} ' + ' '.join(['{:.4f}' for i in range(14)]) + '\n'
        for boxes, clses in zip(batch_box3d, batch_cls):
            label = []
            for box, cls in zip(boxes, clses):
                if coordinate == 'camera':
                    box3d = box
                    box2d = lidar_box3d_to_camera_box(
                        camera_to_lidar_box(box[np.newaxis, :].astype(np.float32), T_VELO_2_CAM, R_RECT_0), cal_projection=False,  P2=P2, T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)[0]
                else:
                    box3d = lidar_to_camera_box(
                        box[np.newaxis, :].astype(np.float32), T_VELO_2_CAM, R_RECT_0)[0]
                    box2d = lidar_box3d_to_camera_box(
                        box[np.newaxis, :].astype(np.float32), cal_projection=False, P2=P2, T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)[0]
                x, y, z, h, w, l, r = box3d
                box3d = [h, w, l, x, y, z, r]
                label.append(template.format(cls, 0, 0, 0, *box2d, *box3d))
            batch_label.append(label)

    return np.array(batch_label)


def cal_anchors(cfg):
    # Output:
    #   anchors: (w, l, 2, 7) x y z h w l q0 q1 q2 q3
    x = np.linspace(cfg.X_MIN, cfg.X_MAX, cfg.FEATURE_WIDTH)
    y = np.linspace(cfg.Y_MIN, cfg.Y_MAX, cfg.FEATURE_HEIGHT)
    cx, cy = np.meshgrid(x, y)
    # all is (w, l, 2)cal_anchors
    cx = np.tile(cx[..., np.newaxis], 2)
    cy = np.tile(cy[..., np.newaxis], 2)
    cz = np.ones_like(cx) * cfg.ANCHOR_Z
    w = np.ones_like(cx) * cfg.ANCHOR_W
    l = np.ones_like(cx) * cfg.ANCHOR_L
    h = np.ones_like(cx) * cfg.ANCHOR_H
    r = np.ones_like(cx)
    r[..., 0] = 0  # 0
    r[..., 1] = 90 / 180 * np.pi  # 90

    # 7*(w,l,2) -> (w, l, 2, 7)
    anchors = np.stack([cx, cy, cz, h, w, l, r], axis=-1)

    return anchors

def quat_to_mat(quat):
    q = quat.copy()
    q=np.array(q)
    n = np.dot(q, q)
    if n < np.finfo(q.dtype).eps:
        rot_matrix=np.identity(4)
        return rot_matrix
    q = q * np.sqrt(2.0 / n)
    q = np.outer(q, q)
    rot_matrix = np.array(
        [[1.0 - q[2, 2] - q[3, 3], q[1, 2] + q[3, 0], q[1, 3] - q[2, 0]],
         [q[1, 2] - q[3, 0], 1.0 - q[1, 1] - q[3, 3], q[2, 3] + q[1, 0]],
         [q[1, 3] + q[2, 0], q[2, 3] - q[1, 0], 1.0 - q[1, 1] - q[2, 2]]],
        dtype=q.dtype)
    return rot_matrix

def mat_to_ang(R):
    q = np.zeros(4)
    K = np.zeros([4, 4])
    K[0, 0] = 1 / 3 * (R[0, 0] - R[1, 1] - R[2, 2])
    K[0, 1] = 1 / 3 * (R[1, 0] + R[0, 1])
    K[0, 2] = 1 / 3 * (R[2, 0] + R[0, 2])
    K[0, 3] = 1 / 3 * (R[1, 2] - R[2, 1])
    K[1, 0] = 1 / 3 * (R[1, 0] + R[0, 1])
    K[1, 1] = 1 / 3 * (R[1, 1] - R[0, 0] - R[2, 2])
    K[1, 2] = 1 / 3 * (R[2, 1] + R[1, 2])
    K[1, 3] = 1 / 3 * (R[2, 0] - R[0, 2])
    K[2, 0] = 1 / 3 * (R[2, 0] + R[0, 2])
    K[2, 1] = 1 / 3 * (R[2, 1] + R[1, 2])
    K[2, 2] = 1 / 3 * (R[2, 2] - R[0, 0] - R[1, 1])
    K[2, 3] = 1 / 3 * (R[0, 1] - R[1, 0])
    K[3, 0] = 1 / 3 * (R[1, 2] - R[2, 1])
    K[3, 1] = 1 / 3 * (R[2, 0] - R[0, 2])
    K[3, 2] = 1 / 3 * (R[0, 1] - R[1, 0])
    K[3, 3] = 1 / 3 * (R[0, 0] + R[1, 1] + R[2, 2])
    D, V = np.linalg.eig(K)
    pp = 0
    for i in range(1, 4):
        if (D[i] > D[pp]):
            pp = i
    q = V[:, pp]
    x = q[3]
    y = q[0]
    z = q[1]
    w = q[2]
    rol = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))  # the rol is the yaw angle!
    # pith = math.asin(2*(w*y-z*z))
    # yaw = math.atan2(2*(w*z+x*y),1-2*(z*z+y*y))

    return rol

def gt_boxes3d_to_yaw(batch_boxes, T_VELO_2_CAM):
    # Input: (N, N', 10)
    # Output: (N, N', 7)
    print(f'raw batch boxes:{batch_boxes}')
    batch_boxes_yaw = []
    for boxes in batch_boxes:
        boxes_yaw = []
        for box in boxes:
            center_point = batch_boxes[i, j, 0:3]
            center_point = np.matmul(T_VELO_2_CAM, center_point)

            quaternion = batch_boxes[i, j, -3:-1]
            rotation_mat = quat_to_mat(quaternion)
            rotation_mat = np.matmul(T_VELO_2_CAM, rotation_mat)
            yaw = mat_to_ang(rotation_mat)

            box_yaw = np.vstack(center_point, batch_boxes[i, j, 3:6], yaw)
            boxes_yaw.append(box_yaw)

        print(f'boxes:{len(boxes_yaw)}')
        batch_boxes_yaw.append(np.array(boxes_yaw).reshape(-1, 7))

    print(f'batch boxes:{len(batch_boxes_yaw)}')

    return batch_boxes_yaw

def cal_rpn_target(labels, T_VELO_2_CAM, feature_map_shape, anchors, cls='Car', coordinate='lidar'):
    # Input:
    #   labels: (N, N')
    #   feature_map_shape: (w, l)
    #   anchors: (w, l, 2, 7)
    # Output:
    #   pos_equal_one (N, w, l, 2)
    #   neg_equal_one (N, w, l, 2)
    #   targets (N, w, l, 14)
    # attention: cal IoU on birdview
    batch_size = labels.shape[0]
    batch_gt_boxes3d = label_to_gt_box3d(labels, cls=cls)
    # projection gt_boxes3d from 10 dimension to 7 dimension (x y z h w l q0-3 -> x y z h w l r)
    batch_gt_boxes3d = gt_boxes3d_to_yaw(batch_gt_boxes3d, T_VELO_2_CAM)
    # defined in eq(1) in 2.2
    anchors_reshaped = anchors.reshape(-1, 7)
    anchors_d = np.sqrt(anchors_reshaped[:, 4]**2 + anchors_reshaped[:, 5]**2)
    pos_equal_one = np.zeros((batch_size, *feature_map_shape, 2), dtype=np.float32)
    neg_equal_one = np.zeros((batch_size, *feature_map_shape, 2), dtype=np.float32)
    targets = np.zeros((batch_size, *feature_map_shape, 14), dtype=np.float32)

    for batch_id in range(batch_size):
        # BOTTLENECK
        anchors_standup_2d = anchor_to_standup_box2d(
            anchors_reshaped[:, [0, 1, 4, 5]])
        # BOTTLENECK
        gt_standup_2d = corner_to_standup_box2d(center_to_corner_box2d(
            batch_gt_boxes3d[batch_id][:, [0, 1, 4, 5, 6]], coordinate=coordinate))

        iou = bbox_overlaps(
            np.ascontiguousarray(anchors_standup_2d).astype(np.float32),
            np.ascontiguousarray(gt_standup_2d).astype(np.float32),
        )
        # iou = cal_box3d_iou(
        #     anchors_reshaped,
        #     batch_gt_boxes3d[batch_id]
        # )

        # find anchor with highest iou(iou should also > 0)
        id_highest = np.argmax(iou.T, axis=1)
        id_highest_gt = np.arange(iou.T.shape[0])
        mask = iou.T[id_highest_gt, id_highest] > 0
        id_highest, id_highest_gt = id_highest[mask], id_highest_gt[mask]

        # find anchor iou > cfg.XXX_POS_IOU
        id_pos, id_pos_gt = np.where(iou > cfg.RPN_POS_IOU)

        # find anchor iou < cfg.XXX_NEG_IOU
        id_neg = np.where(np.sum(iou < cfg.RPN_NEG_IOU,
                                 axis=1) == iou.shape[1])[0]

        id_pos = np.concatenate([id_pos, id_highest])
        id_pos_gt = np.concatenate([id_pos_gt, id_highest_gt])

        # TODO: uniquify the array in a more scientific way
        id_pos, index = np.unique(id_pos, return_index=True)
        id_pos_gt = id_pos_gt[index]
        id_neg.sort()

        # cal the target and set the equal one
        index_x, index_y, index_z = np.unravel_index(
            id_pos, (*feature_map_shape, 2))
        pos_equal_one[batch_id, index_x, index_y, index_z] = 1

        # ATTENTION: index_z should be np.array
        targets[batch_id, index_x, index_y, np.array(index_z) * 7] = (
            batch_gt_boxes3d[batch_id][id_pos_gt, 0] - anchors_reshaped[id_pos, 0]) / anchors_d[id_pos]
        targets[batch_id, index_x, index_y, np.array(index_z) * 7 + 1] = (
            batch_gt_boxes3d[batch_id][id_pos_gt, 1] - anchors_reshaped[id_pos, 1]) / anchors_d[id_pos]
        targets[batch_id, index_x, index_y, np.array(index_z) * 7 + 2] = (
            batch_gt_boxes3d[batch_id][id_pos_gt, 2] - anchors_reshaped[id_pos, 2]) / cfg.ANCHOR_H
        targets[batch_id, index_x, index_y, np.array(index_z) * 7 + 3] = np.log(
            batch_gt_boxes3d[batch_id][id_pos_gt, 3] / anchors_reshaped[id_pos, 3])
        targets[batch_id, index_x, index_y, np.array(index_z) * 7 + 4] = np.log(
            batch_gt_boxes3d[batch_id][id_pos_gt, 4] / anchors_reshaped[id_pos, 4])
        targets[batch_id, index_x, index_y, np.array(index_z) * 7 + 5] = np.log(
            batch_gt_boxes3d[batch_id][id_pos_gt, 5] / anchors_reshaped[id_pos, 5])
        targets[batch_id, index_x, index_y, np.array(index_z) * 7 + 6] = (
            batch_gt_boxes3d[batch_id][id_pos_gt, 6] - anchors_reshaped[id_pos, 6])

        index_x, index_y, index_z = np.unravel_index(
            id_neg, (*feature_map_shape, 2))
        neg_equal_one[batch_id, index_x, index_y, index_z] = 1
        # to avoid a box be pos/neg in the same time
        index_x, index_y, index_z = np.unravel_index(
            id_highest, (*feature_map_shape, 2))
        neg_equal_one[batch_id, index_x, index_y, index_z] = 0

    return pos_equal_one, neg_equal_one, targets


# BOTTLENECK
def delta_to_boxes3d(deltas, anchors, coordinate='lidar'):
    # Input:
    #   deltas: (N, w, l, 14)
    #   feature_map_shape: (w, l)
    #   anchors: (w, l, 2, 7)

    # Ouput:
    #   boxes3d: (N, w*l*2, 7)
    anchors_reshaped = anchors.reshape(-1, 7)
    deltas = deltas.reshape(deltas.shape[0], -1, 7)
    anchors_d = np.sqrt(anchors_reshaped[:, 4]**2 + anchors_reshaped[:, 5]**2)
    boxes3d = np.zeros_like(deltas)
    boxes3d[..., [0, 1]] = deltas[..., [0, 1]] * \
        anchors_d[:, np.newaxis] + anchors_reshaped[..., [0, 1]]
    boxes3d[..., [2]] = deltas[..., [2]] * \
        cfg.ANCHOR_H + anchors_reshaped[..., [2]]
    boxes3d[..., [3, 4, 5]] = np.exp(
        deltas[..., [3, 4, 5]]) * anchors_reshaped[..., [3, 4, 5]]
    boxes3d[..., 6] = deltas[..., 6] + anchors_reshaped[..., 6]

    return boxes3d


def point_transform(points, tx, ty, tz, rx=0, ry=0, rz=0):
    # Input:
    #   points: (N, 3)
    #   rx/y/z: in radians
    # Output:
    #   points: (N, 3)
    N = points.shape[0]
    points = np.hstack([points, np.ones((N, 1))])

    mat1 = np.eye(4)
    mat1[3, 0:3] = tx, ty, tz
    points = np.matmul(points, mat1)

    if rx != 0:
        mat = np.zeros((4, 4))
        mat[0, 0] = 1
        mat[3, 3] = 1
        mat[1, 1] = np.cos(rx)
        mat[1, 2] = -np.sin(rx)
        mat[2, 1] = np.sin(rx)
        mat[2, 2] = np.cos(rx)
        points = np.matmul(points, mat)

    if ry != 0:
        mat = np.zeros((4, 4))
        mat[1, 1] = 1
        mat[3, 3] = 1
        mat[0, 0] = np.cos(ry)
        mat[0, 2] = np.sin(ry)
        mat[2, 0] = -np.sin(ry)
        mat[2, 2] = np.cos(ry)
        points = np.matmul(points, mat)

    if rz != 0:
        mat = np.zeros((4, 4))
        mat[2, 2] = 1
        mat[3, 3] = 1
        mat[0, 0] = np.cos(rz)
        mat[0, 1] = -np.sin(rz)
        mat[1, 0] = np.sin(rz)
        mat[1, 1] = np.cos(rz)
        points = np.matmul(points, mat)

    return points[:, 0:3]


def box_transform(boxes, tx, ty, tz, r=0, coordinate='lidar'):
    # Input:
    #   boxes: (N, 10) x y z h w l q0-3
    # Output:
    #   boxes: (N, 10) x y z h w l q0-3
    boxes_corner = center_to_corner_box3d(
        boxes, coordinate=coordinate)  # (N, 8, 3)
    for idx in range(len(boxes_corner)):
        if coordinate == 'lidar':
            boxes_corner[idx] = point_transform(
                boxes_corner[idx], tx, ty, tz, rz=r)
        else:
            boxes_corner[idx] = point_transform(
                boxes_corner[idx], tx, ty, tz, ry=r)

    return corner_to_center_box3d(boxes_corner, coordinate=coordinate)


def cal_iou2d(box1, box2, T_VELO_2_CAM=None, R_RECT_0=None):
    # Input: 
    #   box1/2: x, y, w, l, r
    # Output :
    #   iou
    buf1 = np.zeros((cfg.INPUT_HEIGHT, cfg.INPUT_WIDTH, 3))
    buf2 = np.zeros((cfg.INPUT_HEIGHT, cfg.INPUT_WIDTH, 3))
    tmp = center_to_corner_box2d(np.array([box1, box2]), coordinate='lidar', T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)
    box1_corner = batch_lidar_to_bird_view(tmp[0]).astype(np.int32)
    box2_corner = batch_lidar_to_bird_view(tmp[1]).astype(np.int32)
    buf1 = cv2.fillConvexPoly(buf1, box1_corner, color=(1,1,1))[..., 0]
    buf2 = cv2.fillConvexPoly(buf2, box2_corner, color=(1,1,1))[..., 0]
    indiv = np.sum(np.absolute(buf1-buf2))
    share = np.sum((buf1 + buf2) == 2)
    if indiv == 0:
        return 0.0 # when target is out of bound
    return share / (indiv + share)

def cal_z_intersect(cz1, h1, cz2, h2):
    b1z1, b1z2 = cz1 - h1 / 2, cz1 + h1 / 2
    b2z1, b2z2 = cz2 - h2 / 2, cz2 + h2 / 2
    if b1z1 > b2z2 or b2z1 > b1z2:
        return 0
    elif b2z1 <= b1z1 <= b2z2:
        if b1z2 <= b2z2:
            return h1 / h2
        else:
            return (b2z2 - b1z1) / (b1z2 - b2z1)
    elif b1z1 < b2z1 < b1z2:
        if b2z2 <= b1z2:
            return h2 / h1
        else:
            return (b1z2 - b2z1) / (b2z2 - b1z1)


def cal_iou3d(box1, box2, T_VELO_2_CAM=None, R_RECT_0=None):
    # Input:
    #   box1/2: x, y, z, h, w, l, r
    # Output:
    #   iou
    buf1 = np.zeros((cfg.INPUT_HEIGHT, cfg.INPUT_WIDTH, 3))
    buf2 = np.zeros((cfg.INPUT_HEIGHT, cfg.INPUT_WIDTH, 3))
    tmp = center_to_corner_box2d(np.array([box1[[0,1,4,5,6]], box2[[0,1,4,5,6]]]), coordinate='lidar', T_VELO_2_CAM=T_VELO_2_CAM, R_RECT_0=R_RECT_0)
    box1_corner = batch_lidar_to_bird_view(tmp[0]).astype(np.int32)
    box2_corner = batch_lidar_to_bird_view(tmp[1]).astype(np.int32)
    buf1 = cv2.fillConvexPoly(buf1, box1_corner, color=(1,1,1))[..., 0]
    buf2 = cv2.fillConvexPoly(buf2, box2_corner, color=(1,1,1))[..., 0]
    share = np.sum((buf1 + buf2) == 2)
    area1 = np.sum(buf1)
    area2 = np.sum(buf2)
    
    z1, h1, z2, h2 = box1[2], box1[3], box2[2], box2[3]
    z_intersect = cal_z_intersect(z1, h1, z2, h2)

    return share * z_intersect / (area1 * h1 + area2 * h2 - share * z_intersect)


def cal_box3d_iou(boxes3d, gt_boxes3d, cal_3d=0, T_VELO_2_CAM=None, R_RECT_0=None):
    # Inputs:
    #   boxes3d: (N1, 7) x,y,z,h,w,l,r
    #   gt_boxed3d: (N2, 7) x,y,z,h,w,l,r
    # Outputs:
    #   iou: (N1, N2)
    N1 = len(boxes3d)
    N2 = len(gt_boxes3d)
    output = np.zeros((N1, N2), dtype=np.float32)

    for idx in range(N1):
        for idy in range(N2):
            if cal_3d:
                output[idx, idy] = float(
                    cal_iou3d(boxes3d[idx], gt_boxes3d[idy]), T_VELO_2_CAM, R_RECT_0)
            else:
                output[idx, idy] = float(
                    cal_iou2d(boxes3d[idx, [0, 1, 4, 5, 6]], gt_boxes3d[idy, [0, 1, 4, 5, 6]], T_VELO_2_CAM, R_RECT_0))

    return output


def cal_box2d_iou(boxes2d, gt_boxes2d, T_VELO_2_CAM=None, R_RECT_0=None):
    # Inputs:
    #   boxes2d: (N1, 5) x,y,w,l,r
    #   gt_boxes2d: (N2, 5) x,y,w,l,r
    # Outputs:
    #   iou: (N1, N2)
    N1 = len(boxes2d)
    N2 = len(gt_boxes2d)
    output = np.zeros((N1, N2), dtype=np.float32)
    for idx in range(N1):
        for idy in range(N2):
            output[idx, idy] = cal_iou2d(boxes2d[idx], gt_boxes2d[idy], T_VELO_2_CAM, R_RECT_0)

    return output
