import numpy as np
import matplotlib.pyplot as plt
import os
from os import path
from glob import glob
from natsort import natsorted
from tqdm import tqdm
import re
import ast
import rawpy
import cv2
import argparse
import multiprocessing

def parse_color_correction_gains(data_string):
    red_pattern = r"R:\s?[0-9]+(((,|\.))[0-9]+)?"
    green_even_pattern = r"G_even:\s?[0-9]+(((,|\.))[0-9]+)?"
    green_odd_pattern = r"G_odd:\s?[0-9]+(((,|\.))[0-9]+)?"
    blue_pattern = r"B:\s?[0-9]+(((,|\.))[0-9]+)?"

    R_gain = float(re.search(red_pattern, data_string).group().split(':')[-1].strip().replace(',', '.'))
    G_even_gain = float(re.search(green_even_pattern, data_string).group().split(':')[-1].strip().replace(',', '.'))
    G_odd_gain = float(re.search(green_odd_pattern, data_string).group().split(':')[-1].strip().replace(',', '.'))
    B_gain = float(re.search(blue_pattern, data_string).group().split(':')[-1].strip().replace(',', '.'))
    color_correction_gains = np.array([R_gain, G_even_gain, G_odd_gain, B_gain], dtype=np.float32)

    return color_correction_gains

def parse_ccm(data_string):
    ccm = np.array([eval(x.group()) for x in re.finditer(r"[-+]?\d+/\d+|[-+]?\d+\.\d+|[-+]?\d+", data_string)])
    ccm = ccm.reshape(3,3)
    return ccm

def parse_tonemap(data_string):
    channels = re.findall(r'(R|G|B):\[(.*?)\]', data_string)
    result_array = np.zeros((3, len(channels[0][1].split('),')), 2))

    for i, (_, channel_data) in enumerate(channels):
        pairs = channel_data.split('),')
        for j, pair in enumerate(pairs):
            x, y = map(float, re.findall(r'([\d\.]+)', pair))
            result_array[i, j] = (x, y)
    return result_array

def parse_metadata_string(metadata_string):
    keys = re.findall(r'<KEY>android.(.*?)<ENDKEY>', metadata_string)
    values = re.findall(r'<VALUE>(.*?)<ENDVALUE>', metadata_string)
    
    metadata_dict = {}

    for key, value in zip(keys, values):
        # Convert simple values to the appropriate type
        if value == 'true':
            value = True
        elif value == 'false':
            value = False
        elif re.fullmatch(r'[0-9]+', value):
            value = int(value)
        elif re.fullmatch(r'[0-9E]*\.[0-9E]+', value):
            value = float(value)
        metadata_dict[key] = value

    return metadata_dict

def write_mp4(frames, video_name='test.mp4', fps=24.0):
    if len(frames[0].shape) == 3:
        height, width, layers = frames[0].shape
    else:
        height, width = frames[0].shape
        layers = 1
    
    frames = frames - frames.min()
    frames = (frames/frames.max() * 255).astype(np.uint8)

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    video = cv2.VideoWriter(video_name, fourcc, fps, (width,height))

    if layers == 4: # RGBA -> BGR
        for frame in tqdm(frames):
            video.write(frame[:,:,[2,1,0]])
    elif layers == 3: # RGB -> BGR
        for frame in tqdm(frames):
            video.write(frame[:,:,[2,1,0]])
    elif layers == 1: # grayscale
        for frame in tqdm(frames):
            video.write(frame[:,:,None].repeat(3,2))
    else:
        raise Exception("Unsupported array size.")

    cv2.destroyAllWindows()
    video.release()

def process_motion(npz_file, motion_path):
    # Load motion data
    with open(motion_path, mode='rb') as file:
            motion = str(file.read())

    motion = motion.split("<ENDACC>")
    acceleration = motion[:-1]
    quaternion = motion[-1].split("<ENDROT>")

    # Read acceleration values

    acceleration_timestamps = []
    acceleration_values = []

    for acc in acceleration:
        acc = re.sub("[^-0-9.,E]", "", acc).split(',')

        acceleration_timestamps.append(int(acc[0]))
        acceleration_values.append([float(x) for x in acc[1:]])

    # Android acceleration, in portrait mode, follows the following convention:
    # +x: right along short side of screen, towards power button
    # +y: up along long side of screen, towards front facing camera
    # +z: out of screen, towards your face

    acceleration_timestamps = np.array(acceleration_timestamps)/1e9
    acceleration_values = np.array(acceleration_values)

    quaternion_timestamps = []
    quaternion_values = []

    for rot in quaternion[:-1]:
        rot = re.sub("[^-0-9.,E]", "", rot).split(',')

        quaternion_timestamps.append(int(rot[0]))
        quaternion_values.append([float(x) for x in rot[1:]])

    quaternion_timestamps = np.array(quaternion_timestamps)/1e9
    quaternion_values = np.array(quaternion_values)

    quaternion_timestamps, unique_quaternion_indices = np.unique(quaternion_timestamps, return_index=True)
    quaternion_values = quaternion_values[unique_quaternion_indices]

    # resample acceleration values to match quaternion timestamps
    interpolated_acceleration_values = np.empty((len(quaternion_timestamps), 3))

    for i in range(3): # x, y, z
        interpolated_acceleration_values[:, i] = np.interp(quaternion_timestamps, acceleration_timestamps, acceleration_values[:, i])


    motion = {'timestamp': quaternion_timestamps,
              'quaternion': quaternion_values,
              'acceleration': interpolated_acceleration_values
            }
    
    npz_file['motion'] = motion

def process_metadata(npz_file, metadata_paths):

    for metadata_path in metadata_paths:
        with open(metadata_path, mode='rb') as file:
            metadata_string = str(file.read())

        metadata_dict = parse_metadata_string(metadata_string)

        fx,fy,cx,cy,s = list(metadata_dict['lens.intrinsicCalibration'].split(','))
        intrinsics = np.array([[fx, 0,  0],
                            [s,  fy, 0],
                            [cx, cy, 1]], dtype=np.float32)

        frame_count = int(metadata_path.split("_")[-1].strip(".bin"))

        timestamp = metadata_dict['sensor.timestamp']/1e9 # convert to seconds
        ISO = metadata_dict['sensor.sensitivity']
        exposure_time = metadata_dict['sensor.exposureTime']/1e9 # convert to seconds
        aperture = metadata_dict['lens.aperture']
        # BGGR bayer black-level
        blacklevel = np.array(list(metadata_dict['sensor.dynamicBlackLevel'].split(',')), np.float32)
        whitelevel = metadata_dict['sensor.dynamicWhiteLevel']
        focal_length = metadata_dict['lens.focalLength']
        focus_distance = metadata_dict['lens.focusDistance']

        # Extract per-channel shading maps
        shade_map = metadata_dict['statistics.lensShadingCorrectionMap']

        shade_map = shade_map.replace("R:","|")
        shade_map = shade_map.replace("G_even:","|")
        shade_map = shade_map.replace("G_odd:","|")
        shade_map = shade_map.replace("B:","|")
        shade_map = re.sub('[^0-9.,\[\]\|]', '', shade_map)

        R,G1,G2,B = shade_map.split("|")[1:]
        R = np.array(ast.literal_eval(R)) # match portrait rotation
        G1 = np.array(ast.literal_eval(G1))
        G2 = np.array(ast.literal_eval(G2))
        B = np.array(ast.literal_eval(B))   

        shade_map = np.stack([R,G1,G2,B], axis=-1)

        lens_distortion = metadata_dict['lens.distortion']
        lens_distortion = lens_distortion = np.array([float(f) for f in lens_distortion.split(',')])

        tonemap_curve = metadata_dict['tonemap.curve']
        tonemap_curve = parse_tonemap(tonemap_curve)

        color_correction_gains = metadata_dict['colorCorrection.gains']
        color_correction_gains = parse_color_correction_gains(color_correction_gains)

        ccm = metadata_dict['colorCorrection.transform']
        ccm = parse_ccm(ccm)

        raw_frame = {'android': metadata_dict, # original metadata
                    'frame_count': frame_count,
                    'timestamp': timestamp,
                    'ISO': ISO,
                    'exposure_time': exposure_time,
                    'aperture': aperture,
                    'blacklevel': blacklevel,
                    'whitelevel': whitelevel,
                    'focal_length': focal_length,
                    'focus_distance': focus_distance,
                    'intrinsics': intrinsics,
                    'shade_map': shade_map,
                    'lens_distortion': lens_distortion,
                    'tonemap_curve': tonemap_curve,
                    'color_correction_gains': color_correction_gains,
                    'ccm': ccm}

        npz_file[f'raw_{frame_count}'] = raw_frame
        
    npz_file['num_raw_frames'] = frame_count + 1

def process_characteristics(npz_file, characteristics_path):

    with open(characteristics_path, mode='rb') as file:
        characteristics_string= str(file.read())

    characteristics_dict = parse_metadata_string(characteristics_string)

    # 0: RGGB, 1: GRBG, 2: GBRG, 3: BGGR
    color_filter_arrangement = characteristics_dict['sensor.info.colorFilterArrangement']
    pose_reference = characteristics_dict['lens.poseReference']
    pose_rotation = characteristics_dict['lens.poseRotation']
    pose_rotation = np.array([float(f) for f in pose_rotation.split(',')])
    pose_translation = characteristics_dict['lens.poseTranslation']
    pose_translation = np.array([float(f) for f in pose_translation.split(',')])
    aperture = characteristics_dict['lens.info.availableApertures']
    focal_length = characteristics_dict['lens.info.availableFocalLengths']
    minimum_focus_distance = characteristics_dict['lens.info.minimumFocusDistance']
    hyperfocal_distance = characteristics_dict['lens.info.hyperfocalDistance']

    characteristics = {'android' : characteristics_dict,
                    'color_filter_arrangement' : color_filter_arrangement,
                    'pose_reference' : pose_reference,
                    'pose_rotation' : pose_rotation,
                    'pose_translation' : pose_translation,
                    'aperture' : aperture,
                    'focal_length' : focal_length,
                    'minimum_focus_distance' : minimum_focus_distance,
                    'hyperfocal_distance' : hyperfocal_distance}

    npz_file["characteristics"] = characteristics


def process_raw(npz_file, raw_paths):

    for raw_path in raw_paths:
        frame_count = int(raw_path.split("_")[-1].strip(".dng"))

        raw = rawpy.imread(raw_path).raw_image
        height, width = raw.shape

        if f'raw_{frame_count}' not in npz_file.keys():
            npz_file[f'raw_{frame_count}'] = {}
        
        npz_file[f'raw_{frame_count}']['raw'] = raw
        npz_file[f'raw_{frame_count}']['height'] = height
        npz_file[f'raw_{frame_count}']['width'] = width
    
# Sort raw and metadata files by timestamp, remove dropped frames or metadata
def sort_and_filter_files(npz_file):
    # all the raw images or metadata we received
    raw_keys = [key for key in npz_file.keys() if 'raw_' in key and 'num_raw_frames' not in key]

    raw_keys_matched = []
    for raw_key in raw_keys:
        # we received both raw and metadata for this frame
        if 'raw' in npz_file[raw_key].keys() and 'timestamp' in npz_file[raw_key].keys():
            raw_keys_matched.append(raw_key)
    
    timestamps = np.array([npz_file[raw_key]['timestamp'] for raw_key in raw_keys_matched])
    sorted_indices = np.argsort(timestamps)
    raw_keys_matched = np.array(raw_keys_matched)[sorted_indices] # sort by timestamp

    # make new dict with sorted raw and metadata
    npy_file_sorted = {}
    for frame_count, raw_key in enumerate(raw_keys_matched):
        npy_file_sorted[f'raw_{frame_count}'] = npz_file[raw_key]
        npy_file_sorted[f'raw_{frame_count}']['frame_count'] = frame_count
    
    npy_file_sorted['num_raw_frames'] = len(raw_keys_matched)
    # npy_file_sorted['motion'] = npz_file['motion']
    npy_file_sorted['characteristics'] = npz_file['characteristics']

    return npy_file_sorted

def colorize_frame(npz_file, frame, downsample_factor=1, max_brightness=1.0):
    color_filter_arrangement = npz_file['characteristics']['color_filter_arrangement']
    color_correction_gains = npz_file['raw_0']['color_correction_gains']
    ccm = npz_file['raw_0']['ccm']
    tonemap_curve = npz_file['raw_0']['tonemap_curve']
    blacklevel = npz_file['raw_0']['blacklevel'][0]
    whitelevel = npz_file[f'raw_0']['whitelevel']

    top_left = frame[0::2*downsample_factor,0::2*downsample_factor]
    top_right = frame[0::2*downsample_factor,1::2*downsample_factor]
    bottom_left = frame[1::2*downsample_factor,0::2*downsample_factor]
    bottom_right = frame[1::2*downsample_factor,1::2*downsample_factor]

    # figure out color channels
    if color_filter_arrangement == 0: # RGGB
        R, G1, G2, B = top_left, top_right, bottom_left, bottom_right
    elif color_filter_arrangement == 1: # GRBG
        G1, R, B, G2 = top_left, top_right, bottom_left, bottom_right
    elif color_filter_arrangement == 2: # GBRG
        G1, B, R, G2 = top_left, top_right, bottom_left, bottom_right
    elif color_filter_arrangement == 3: # BGGR
        B, G1, G2, R = top_left, top_right, bottom_left, bottom_right

    R = ((R - blacklevel) / (whitelevel - blacklevel) * color_correction_gains[0]) 
    G = ((G1 - blacklevel) / (whitelevel - blacklevel) * color_correction_gains[1])
    B = ((B - blacklevel) / (whitelevel - blacklevel) * color_correction_gains[3]) 

    rgb_frame = np.stack([R,G,B], axis=0)
    height, width = rgb_frame.shape[1:]

    rgb_frame = ccm @ rgb_frame.reshape(3,-1)
    rgb_frame = rgb_frame.reshape(3, height, width)
    
    for i in range(3):
        x_vals, y_vals = tonemap_curve[i][:, 0], tonemap_curve[i][:, 1]
        rgb_frame[i] = np.interp(rgb_frame[i], x_vals, y_vals)

    # rearrange back to HWC
    rgb_frame = np.moveaxis(rgb_frame, 0, -1)
    rgb_frame = rgb_frame/max_brightness
    rgb_frame = np.clip(rgb_frame, 0, 1)

    return rgb_frame

# get low dynamic range frames
def get_LDR_frames(npy_file):
    frames = np.array([npy_file[f'raw_{i}']['raw'] for i in range(npy_file['num_raw_frames'])])
    max_brightness = np.percentile(colorize_frame(npy_file, frames[0], 2), 98)
    frames = np.array([colorize_frame(npy_file, frame, 2, max_brightness) for frame in frames])
    frames = np.rot90(frames, 3, axes=(1,2)) # rotate to portrait mode
    return frames

def has_subfolders(folder):
    for _, dirnames, _ in os.walk(folder):
        if len(dirnames) > 0:
            return True
    return False

def process_bundle(bundle_path, base_path):
    try:
        motion_path = path.join(bundle_path, "MOTION.bin")
        characteristics_path = path.join(bundle_path, "CHARACTERISTICS.bin")
        raw_paths = natsorted(glob(path.join(bundle_path, "IMG*.dng")))
        metadata_paths = natsorted(glob(path.join(bundle_path, "IMG*.bin")))
        assert len(raw_paths) == len(metadata_paths) # matched data

        npy_file = {}
        npy_file["bundle_name"] = path.basename(bundle_path)

        print(f"Processing: {bundle_path}")
        # process_motion(npy_file, motion_path)
        process_characteristics(npy_file, characteristics_path)
        process_metadata(npy_file, metadata_paths)
        process_raw(npy_file, raw_paths)
        npy_file = sort_and_filter_files(npy_file)

        # Create a new folder to save the processed data
        if has_subfolders(base_path): # add processed_ prefix to parent folder
            parent, child = path.dirname(bundle_path), path.basename(bundle_path)
            parent = path.join(path.dirname(parent), "processed_" + path.basename(parent))
            save_path = path.join(parent, child)
        else:
            save_path = path.join(path.dirname(bundle_path), "processed_" + path.basename(bundle_path))

        os.makedirs(save_path, exist_ok=True)

        # Save all data to a single npy file
        # print(f"Saving to: {npy_save_path}")
        # npy_save_path = path.join(save_path, "frame_bundle.npy")
        # np.save(npy_save_path, npy_file)

        print("Saving metadata...")

        # Save timestamps into timestamps.txt
        with open(path.join(save_path, "timestamps.txt"), "w") as f:
            for i in range(npy_file['num_raw_frames']):
                f.write(f"{npy_file[f'raw_{i}']['timestamp']}\n")    

        # Save exposure times into exposure_times.txt
        with open(path.join(save_path, "exposure_times.txt"), "w") as f:
            for i in range(npy_file['num_raw_frames']):
                f.write(f"{npy_file[f'raw_{i}']['exposure_time']}\n")

        # Save ISOs into ISOs.txt
        with open(path.join(save_path, "ISOs.txt"), "w") as f:
            for i in range(npy_file['num_raw_frames']):
                f.write(f"{npy_file[f'raw_{i}']['ISO']}\n")

        # Save apertures into apertures.txt
        with open(path.join(save_path, "apertures.txt"), "w") as f:
            for i in range(npy_file['num_raw_frames']):
                f.write(f"{npy_file[f'raw_{i}']['aperture']}\n")

        # Save focal lengths into focals.txt
        with open(path.join(save_path, "focals.txt"), "w") as f:
            for i in range(npy_file['num_raw_frames']):
                f.write(f"{npy_file[f'raw_{i}']['focal_length']}\n")
        
        # Save focus distances into focus_distances.txt
        with open(path.join(save_path, "focus_distances.txt"), "w") as f:
            for i in range(npy_file['num_raw_frames']):
                f.write(f"{npy_file[f'raw_{i}']['focus_distance']}\n")

        # Get LDR frames
        ldr_frames = get_LDR_frames(npy_file)

        # Save LDR images to images/ folder, with timestamp as filename
        print("Saving images...")
        os.makedirs(path.join(save_path, "images"), exist_ok=True)
        for i, frame in enumerate(ldr_frames):
            cv2.imwrite(
                path.join(save_path, "images", f"{npy_file[f'raw_{i}']['timestamp']}.jpg"),
                cv2.cvtColor((frame*255).astype(np.uint8), cv2.COLOR_RGB2BGR)
            )

        # Save preview video
        video_save_path = path.join(save_path, "preview.mp4")
        print(f"Saving to: {video_save_path}")
        write_mp4(ldr_frames, video_save_path, fps=15.0)
        print("Done.")

    except Exception as e:
        print(f"Error processing {bundle_path}: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', default=None, type=str, required=True, help='Data directory')
    args = parser.parse_args()

    base_path = args.d  # Store the base directory

    if has_subfolders(base_path):
        bundle_paths = natsorted(glob(os.path.join(base_path, "*/")))
        bundle_paths = [os.path.normpath(bundle_path) for bundle_path in bundle_paths if "processed_2" not in bundle_path]
    else:
        bundle_paths = [os.path.normpath(base_path)]

    num_processes = min(multiprocessing.cpu_count(), 4)

    with multiprocessing.Pool(num_processes) as pool:
        pool.starmap(process_bundle, [(bundle_path, base_path) for bundle_path in bundle_paths])

if __name__ == '__main__':
    main()
