import math
import os
import queue
import threading

import cv2
import numpy as np
import onnxruntime as ort
import torch
from basicsr.utils.download_util import load_file_from_url
from torch.nn import functional as F
from transformers import AutoModel
from tritonclient import http as httpclient

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RealESRGANer:
    """A helper class for upsampling images with RealESRGAN.

    Args:
        scale (int): Upsampling scale factor used in the networks.
            It is usually 2 or 4.
        model_path (str): The path to the pretrained model.
            It can be urls (will first download it automatically).
        model (nn.Module): The defined network. Default: None.
        tile (int): As too large images result in the out of GPU memory issue,
            so this tile option will first crop input images into tiles,
            and then process each of them. Finally, they will be merged
            into one image. 0 denotes for do not use tile. Default: 0.
        tile_pad (int): The pad size for each tile, to remove border artifacts.
            Default: 10.
        pre_pad (int): Pad the input images to avoid border artifacts.
            Default: 10.
        half (float): Whether to use half precision during inference.
            Default: False.
    """

    def __init__(
        self,
        scale,
        model_path,
        dni_weight=None,
        model=None,
        tile=0,
        tile_pad=10,
        pre_pad=10,
        half=False,
        device=None,
        gpu_id=None,
        backend="torch",
        onnx_path=None,
        triton_url=None,
        triton_model_name=None,
        triton_model_version=None,
        outscale=None,
        hf_repository=None,
    ):
        self.scale = scale
        self.tile_size = tile
        self.tile_pad = tile_pad
        self.pre_pad = pre_pad
        self.mod_scale = None
        self.half = half
        self.backend = backend
        self.onnx_path = onnx_path
        self.triton_url = triton_url
        self.triton_model_name = triton_model_name
        self.triton_model_version = triton_model_version
        self.outscale = outscale

        # initialize model torch backend
        if self.backend == "torch":
            if gpu_id is not None and gpu_id != -1:
                if device is None:
                    self.device = torch.device(
                        f"cuda:{gpu_id}" if torch.cuda.is_available() else "cpu"
                    )
                else:
                    self.device = device
            else:
                if device is None:
                    self.device = torch.device("cpu")
                else:
                    self.device = device

            if isinstance(model_path, list):
                # dni
                assert len(model_path) == len(
                    dni_weight
                ), "model_path and dni_weight should have the save length."
                loadnet = self.dni(model_path[0], model_path[1], dni_weight)
            else:
                # if the model_path starts with https,
                # it will first download models to the folder: weights
                if model_path.startswith("https://"):
                    model_path = load_file_from_url(
                        url=model_path,
                        model_dir=os.path.join(ROOT_DIR, "weights"),
                        progress=True,
                        file_name=None,
                    )
                loadnet = torch.load(model_path, map_location=torch.device("cpu"))

            # prefer to use params_ema
            if "params_ema" in loadnet:
                keyname = "params_ema"
            else:
                keyname = "params"
            model.load_state_dict(loadnet[keyname], strict=True)

            model.eval()
            self.net_g = model.to(self.device)
            if self.half:
                self.net_g = self.net_g.half()
            self.dtype = torch.float16 if self.half else torch.float32
        elif self.backend == "onnx":
            self.ort_session = ort.InferenceSession(
                self.onnx_path,
                providers=[
                    "TensorrtExecutionProvider",
                    "CUDAExecutionProvider",
                    "CPUExecutionProvider",
                ],
            )
            self.ort_input_name = self.ort_session.get_inputs()[0].name
            self.ort_output_name = self.ort_session.get_outputs()[0].name
        elif self.backend == "triton":
            self.triton_client = httpclient.InferenceServerClient(url=self.triton_url)
        elif self.backend == "huggingface":
            if gpu_id is not None and gpu_id != -1:
                if device is None:
                    self.device = torch.device(
                        f"cuda:{gpu_id}" if torch.cuda.is_available() else "cpu"
                    )
                else:
                    self.device = device
            else:
                if device is None:
                    self.device = torch.device("cpu")
                else:
                    self.device = device

            model = AutoModel.from_pretrained(hf_repository, trust_remote_code=True)

            model.eval()
            self.net_g = model.to(self.device)
            if self.half:
                self.net_g = self.net_g.half()
            self.dtype = torch.float16 if self.half else torch.float32
        else:
            raise ValueError(f"The {self.backend} backend isn't supported")

    def dni(self, net_a, net_b, dni_weight, key="params", loc="cpu"):
        """Deep network interpolation.

        Paper:
            Deep Network Interpolation for Continuous Imagery Effect Transition
        """
        net_a = torch.load(net_a, map_location=torch.device(loc))
        net_b = torch.load(net_b, map_location=torch.device(loc))
        for k, v_a in net_a[key].items():
            net_a[key][k] = dni_weight[0] * v_a + dni_weight[1] * net_b[key][k]
        return net_a

    def pre_process_numpy(self, img):
        """Pre-process such as pre-pad and mod pad,
        so that the images can be divisible using numpy
        """
        img = np.transpose(img, (2, 0, 1))
        img = np.expand_dims(img, axis=0)
        self.img = img.astype(np.float16) if self.half else img

        # pre_pad
        if self.pre_pad != 0:
            self.img = np.pad(
                self.img,
                ((0, 0), (0, 0), (0, self.pre_pad), (0, self.pre_pad)),
                mode="reflect",
            )

        # mod pad for divisible borders
        if self.scale == 2:
            self.mod_scale = 2
        elif self.scale == 1:
            self.mod_scale = 4
        if self.mod_scale is not None:
            self.mod_pad_h, self.mod_pad_w = 0, 0
            _, _, h, w = self.img.shape
            if h % self.mod_scale != 0:
                self.mod_pad_h = self.mod_scale - h % self.mod_scale
            if w % self.mod_scale != 0:
                self.mod_pad_w = self.mod_scale - w % self.mod_scale
            self.img = np.pad(
                self.img,
                ((0, 0), (0, 0), (0, self.mod_pad_h), (0, self.mod_pad_w)),
                "reflect",
            )

    def pre_process(self, img):
        """Pre-process, such as pre-pad and mod pad,
        so that the images can be divisible
        """
        img = torch.from_numpy(np.transpose(img, (2, 0, 1))).float()
        self.img = img.unsqueeze(0).to(self.device)
        if self.half:
            self.img = self.img.half()

        # pre_pad
        if self.pre_pad != 0:
            self.img = F.pad(self.img, (0, self.pre_pad, 0, self.pre_pad), "reflect")

        # mod pad for divisible borders
        if self.scale == 2:
            self.mod_scale = 2
        elif self.scale == 1:
            self.mod_scale = 4
        if self.mod_scale is not None:
            self.mod_pad_h, self.mod_pad_w = 0, 0
            _, _, h, w = self.img.size()
            if h % self.mod_scale != 0:
                self.mod_pad_h = self.mod_scale - h % self.mod_scale
            if w % self.mod_scale != 0:
                self.mod_pad_w = self.mod_scale - w % self.mod_scale
            self.img = F.pad(
                self.img, (0, self.mod_pad_w, 0, self.mod_pad_h), "reflect"
            )

    def process(self):
        # model inference
        if self.backend == "torch":
            self.output = self.net_g(self.img)
        elif self.backend == "onnx":
            self.output = self.ort_session.run(
                [self.ort_output_name],
                {self.ort_input_name: self.img},
            )[0]
        elif self.backend == "triton":
            dtype = "FP16" if self.half else "FP32"

            inputs = [httpclient.InferInput("lr", self.img.shape, dtype)]
            inputs[0].set_data_from_numpy(self.img, binary_data=True)

            outputs = [httpclient.InferRequestedOutput("hr", binary_data=True)]
            self.output = self.triton_client.infer(
                model_name=self.triton_model_name,
                model_version=str(self.triton_model_version),
                inputs=inputs,
                outputs=outputs,
            ).as_numpy("hr")
        elif self.backend == "huggingface":
            self.output = self.net_g(self.img)
        else:
            raise ValueError(f"The {self.backend} backend isn't supported")

    def tile_process(self):
        """It will first crop input images to tiles,
            and then process each tile.
        Finally, all the processed tiles are merged into one images.

        Modified from: https://github.com/ata4/esrgan-launcher
        """
        if self.backend == "onnx" or self.backend == "triton":
            raise NotImplementedError(
                f"The {self.backend} backend isn't supported for tile process yet"
            )

        batch, channel, height, width = self.img.shape
        output_height = height * self.scale
        output_width = width * self.scale
        output_shape = (batch, channel, output_height, output_width)

        # start with black image
        self.output = self.img.new_zeros(output_shape)
        tiles_x = math.ceil(width / self.tile_size)
        tiles_y = math.ceil(height / self.tile_size)

        # loop over all tiles
        for y in range(tiles_y):
            for x in range(tiles_x):
                # extract tile from input image
                ofs_x = x * self.tile_size
                ofs_y = y * self.tile_size
                # input tile area on total image
                input_start_x = ofs_x
                input_end_x = min(ofs_x + self.tile_size, width)
                input_start_y = ofs_y
                input_end_y = min(ofs_y + self.tile_size, height)

                # input tile area on total image with padding
                input_start_x_pad = max(input_start_x - self.tile_pad, 0)
                input_end_x_pad = min(input_end_x + self.tile_pad, width)
                input_start_y_pad = max(input_start_y - self.tile_pad, 0)
                input_end_y_pad = min(input_end_y + self.tile_pad, height)

                # input tile dimensions
                input_tile_width = input_end_x - input_start_x
                input_tile_height = input_end_y - input_start_y
                tile_idx = y * tiles_x + x + 1
                input_tile = self.img[
                    :,
                    :,
                    input_start_y_pad:input_end_y_pad,
                    input_start_x_pad:input_end_x_pad,
                ]

                # upscale tile
                try:
                    with torch.no_grad():
                        output_tile = self.net_g(input_tile)
                except RuntimeError as error:
                    print("Error", error)
                print(f"\tTile {tile_idx}/{tiles_x * tiles_y}")

                # output tile area on total image
                output_start_x = input_start_x * self.scale
                output_end_x = input_end_x * self.scale
                output_start_y = input_start_y * self.scale
                output_end_y = input_end_y * self.scale

                # output tile area without padding
                output_start_x_tile = (input_start_x - input_start_x_pad) * self.scale
                output_end_x_tile = output_start_x_tile + input_tile_width * self.scale
                output_start_y_tile = (input_start_y - input_start_y_pad) * self.scale
                output_end_y_tile = output_start_y_tile + input_tile_height * self.scale

                # put tile into output image
                self.output[
                    :, :, output_start_y:output_end_y, output_start_x:output_end_x
                ] = output_tile[
                    :,
                    :,
                    output_start_y_tile:output_end_y_tile,
                    output_start_x_tile:output_end_x_tile,
                ]

    def post_process(self):
        # remove extra pad
        if self.mod_scale is not None:
            _, _, h, w = self.output.shape
            self.output = self.output[
                :,
                :,
                0 : h - self.mod_pad_h * self.scale,
                0 : w - self.mod_pad_w * self.scale,
            ]
        # remove prepad
        if self.pre_pad != 0:
            _, _, h, w = self.output.shape
            self.output = self.output[
                :,
                :,
                0 : h - self.pre_pad * self.scale,
                0 : w - self.pre_pad * self.scale,
            ]
        return self.output

    @torch.no_grad()
    def enhance(self, img, alpha_upsampler="realesrgan"):
        h_input, w_input = img.shape[0:2]
        # img: numpy
        img = img.astype(np.float32)
        if np.max(img) > 256:  # 16-bit image
            max_range = 65535
            print("\tInput is a 16-bit image")
        else:
            max_range = 255
        img = img / max_range
        if len(img.shape) == 2:  # gray image
            img_mode = "L"
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
        elif img.shape[2] == 4:  # RGBA image with alpha channel
            img_mode = "RGBA"
            alpha = img[:, :, 3]
            img = img[:, :, 0:3]
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            if alpha_upsampler == "realesrgan":
                alpha = cv2.cvtColor(alpha, cv2.COLOR_GRAY2RGB)
        else:
            img_mode = "RGB"
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        # ---------- process image (without the alpha channel) ---------- #
        if self.backend == "torch":
            self.pre_process(img)
        elif self.backend == "onnx":
            self.pre_process_numpy(img)
        elif self.backend == "triton":
            self.pre_process_numpy(img)
        elif self.backend == "huggingface":
            self.pre_process(img)
        else:
            raise ValueError(f"The {self.backend} backend isn't supported")

        if self.tile_size > 0:
            self.tile_process()
        else:
            self.process()

        output_img = self.post_process()
        if self.backend == "torch":
            output_img = output_img.data.squeeze().float().cpu().clamp_(0, 1).numpy()
        elif self.backend == "onnx":
            output_img = output_img.squeeze().astype(np.float32).clip(0, 1)
        elif self.backend == "triton":
            output_img = output_img.squeeze().astype(np.float32).clip(0, 1)
        elif self.backend == "huggingface":
            output_img = output_img.data.squeeze().float().cpu().clamp_(0, 1).numpy()
        else:
            raise ValueError(f"The {self.backend} backend isn't supported")
        output_img = np.transpose(output_img[[2, 1, 0], :, :], (1, 2, 0))
        if img_mode == "L":
            output_img = cv2.cvtColor(output_img, cv2.COLOR_BGR2GRAY)

        # ------------- process the alpha channel if necessary -------------- #
        if img_mode == "RGBA":
            if alpha_upsampler == "realesrgan":
                if self.backend == "torch":
                    self.pre_process(alpha)
                elif self.backend == "onnx":
                    self.pre_process_numpy(alpha)
                elif self.backend == "triton":
                    self.pre_process_numpy(alpha)
                elif self.backend == "huggingface":
                    self.pre_process(alpha)
                else:
                    raise ValueError(f"The {self.backend} backend isn't supported")

                if self.tile_size > 0:
                    self.tile_process()
                else:
                    self.process()
                output_alpha = self.post_process()

                if self.backend == "torch":
                    output_alpha = output_alpha.data.squeeze()
                    output_alpha = output_alpha.float().cpu().clamp_(0, 1).numpy()
                elif self.backend == "onnx":
                    output_alpha = output_alpha.squeeze().astype(np.float32).clip(0, 1)
                elif self.backend == "triton":
                    output_alpha = output_alpha.squeeze().astype(np.float32).clip(0, 1)
                elif self.backend == "huggingface":
                    output_alpha = output_alpha.data.squeeze()
                    output_alpha = output_alpha.float().cpu().clamp_(0, 1).numpy()
                else:
                    raise ValueError(f"The {self.backend} backend isn't supported")

                output_alpha = np.transpose(output_alpha[[2, 1, 0], :, :], (1, 2, 0))
                output_alpha = cv2.cvtColor(output_alpha, cv2.COLOR_BGR2GRAY)
            else:  # use the cv2 resize for alpha channel
                h, w = alpha.shape[0:2]
                output_alpha = cv2.resize(
                    alpha,
                    (w * self.scale, h * self.scale),
                    interpolation=cv2.INTER_LINEAR,
                )

            # merge the alpha channel
            output_img = cv2.cvtColor(output_img, cv2.COLOR_BGR2BGRA)
            output_img[:, :, 3] = output_alpha

        # ----------------------------- return ----------------------------- #
        if max_range == 65535:  # 16-bit image
            output = (output_img * 65535.0).round().astype(np.uint16)
        else:
            output = (output_img * 255.0).round().astype(np.uint8)

        if self.outscale is not None and self.outscale != float(self.scale):
            output = cv2.resize(
                output,
                (
                    int(w_input * self.outscale),
                    int(h_input * self.outscale),
                ),
                interpolation=cv2.INTER_LANCZOS4,
            )

        return output, img_mode


class PrefetchReader(threading.Thread):
    """Prefetch images.

    Args:
        img_list (list[str]): A image list of image paths to be read.
        num_prefetch_queue (int): Number of prefetch queue.
    """

    def __init__(self, img_list, num_prefetch_queue):
        super().__init__()
        self.que = queue.Queue(num_prefetch_queue)
        self.img_list = img_list

    def run(self):
        for img_path in self.img_list:
            img = cv2.imread(img_path, cv2.IMREAD_UNCHANGED)
            self.que.put(img)

        self.que.put(None)

    def __next__(self):
        next_item = self.que.get()
        if next_item is None:
            raise StopIteration
        return next_item

    def __iter__(self):
        return self


class IOConsumer(threading.Thread):
    def __init__(self, opt, que, qid):
        super().__init__()
        self._queue = que
        self.qid = qid
        self.opt = opt

    def run(self):
        while True:
            msg = self._queue.get()
            if isinstance(msg, str) and msg == "quit":
                break

            output = msg["output"]
            save_path = msg["save_path"]
            cv2.imwrite(save_path, output)
        print(f"IO worker {self.qid} is done.")
