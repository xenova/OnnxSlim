import logging
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import onnx
from onnx import checker

import onnxslim.onnx_graphsurgeon as gs
from onnxslim.misc.font import GREEN, WHITE
from onnxslim.misc.tabulate import SEPARATING_LINE, tabulate
from onnxslim.onnx_graphsurgeon.logger.logger import G_LOGGER

# Configure logging
logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)

# Create a logger
logger = logging.getLogger("ONNXSlim")


def init_logging(verbose=False):
    """Configure the logging settings for the application based on the verbosity level."""
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    if verbose:  # DEBUG
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler(sys.stderr)],
        )
        G_LOGGER.severity = logging.DEBUG
    else:  # ERROR
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler(sys.stderr)],
        )
        G_LOGGER.severity = logging.ERROR

    G_LOGGER.colors = False

    import onnxruntime as ort

    ort.set_default_logger_severity(3)


def format_bytes(size: Union[int, Tuple[int, ...]]) -> str:
    if isinstance(size, int):
        size = (size,)

    units = ["B", "KB", "MB", "GB"]
    formatted_sizes = []

    for size_in_bytes in size:
        unit_index = 0
        while size_in_bytes >= 1024 and unit_index < len(units) - 1:
            size_in_bytes /= 1024
            unit_index += 1

        formatted_size = "{:.2f} {}".format(size_in_bytes, units[unit_index])
        formatted_sizes.append(formatted_size)

    if len(formatted_sizes) == 1:
        return formatted_sizes[0]
    else:
        return f"{formatted_sizes[0]} ({formatted_sizes[1]})"


def onnx_dtype_to_numpy(onnx_dtype: int) -> np.dtype:
    import onnx.mapping as mapping

    return np.dtype(mapping.TENSOR_TYPE_TO_NP_TYPE[onnx_dtype])


def gen_onnxruntime_input_data(
    model: onnx.ModelProto, model_check_inputs: Optional[List[str]] = None
) -> Dict[str, np.ndarray]:
    input_info = {}
    for input_tensor in model.graph.input:
        name = input_tensor.name
        shape = []
        for dim in input_tensor.type.tensor_type.shape.dim:
            if dim.HasField("dim_param"):
                shape.append(dim.dim_param)
            elif dim.HasField("dim_value"):
                shape.append(dim.dim_value)
            else:
                shape.append(None)
        dtype = onnx_dtype_to_numpy(input_tensor.type.tensor_type.elem_type)

        input_info[name] = {"shape": shape, "dtype": dtype}

    if model_check_inputs:
        for model_check_input in model_check_inputs:
            key, value = model_check_input.rsplit(":", 1)
            if value.endswith(".npy"):
                if key not in input_info:
                    raise Exception(
                        f"model_check_input name:{key} not found in model, available keys: {' '.join(input_info.keys())}"
                    )
                data = np.load(value)
                input_info[key] = {"data": data}
            else:
                values_list = [int(val) for val in value.split(",")]
                if key in input_info:
                    input_info[key]["shape"] = values_list
                else:
                    raise Exception(
                        f"model_check_input name:{key} not found in model, available keys: {' '.join(input_info.keys())}"
                    )

    input_data_dict = {}
    for name, info in input_info.items():
        if "data" in info:
            input_data_dict[name] = info["data"]
        else:
            shapes = [shape if (shape != -1 and not isinstance(shape, str)) else 1 for shape in info["shape"]]
            shapes = shapes or [1]
            dtype = info["dtype"]

            if dtype in {np.int32, np.int64}:
                random_data = np.random.randint(10, size=shapes).astype(dtype)
            else:
                random_data = np.random.rand(*shapes).astype(dtype)
            input_data_dict[name] = random_data

    return input_data_dict


def onnxruntime_inference(model: onnx.ModelProto, input_data: dict) -> Dict[str, np.array]:
    import os
    import onnx
    import tempfile
    import onnxruntime as rt

    if model.ByteSize() >= onnx.checker.MAXIMUM_PROTOBUF:
        tmp_dir = tempfile.TemporaryDirectory()
        tmp_path = os.path.join(tmp_dir.name, "tmp.onnx")
        location = os.path.basename(tmp_path) + ".data"
        if os.path.exists(location):
            os.remove(location)
        onnx.save(
            model,
            tmp_path,
            save_as_external_data=True,
            all_tensors_to_one_file=True,
            location=location,
        )
        onnx_model = tmp_path
    else:
        onnx_model = model.SerializeToString()

    sess = rt.InferenceSession(onnx_model, providers=["CPUExecutionProvider"])
    onnx_output = sess.run(None, input_data)

    output_names = [output.name for output in sess.get_outputs()]
    onnx_output = dict(zip(output_names, onnx_output))

    if isinstance(onnx_model, str):
        model = onnx.load(onnx_model)

    return onnx_output, model


def print_model_info_as_table(model_name: str, model_info_list: List[Dict], elapsed_time: float = None):
    """Prints the model information as a formatted table for the given model name and list of model details."""
    assert model_info_list, "model_info_list must contain more than one model info"

    final_op_info = []
    if len(model_info_list) == 1:
        final_op_info.extend(
            (
                ["Model Name", model_name],
                [SEPARATING_LINE],
                ["Op Set ", model_info_list[0]["op_set"]],
            )
        )
    else:
        final_op_info.append(
            ["Model Name", model_name, "Op Set: " + model_info_list[0]["op_set"]] + [""] * (len(model_info_list) - 2)
        )
    final_op_info.extend(
        (
            [SEPARATING_LINE],
            ["Model Info", "Original Model"] + ["Slimmed Model"] * (len(model_info_list) - 1),
            [SEPARATING_LINE] * (len(model_info_list) + 1),
        )
    )
    all_inputs = list(model_info_list[0]["op_input_info"].keys())

    for inputs in all_inputs:
        input_info_list = [f"IN: {inputs}"]
        for model_info in model_info_list:
            inputs_shape = model_info["op_input_info"].get(inputs, "")
            input_info_list.append(inputs_shape)
        final_op_info.append(input_info_list)

    all_outputs = {op_type for model_info in model_info_list for op_type in model_info.get("op_output_info", {})}

    for outputs in all_outputs:
        output_info_list = [f"OUT: {outputs}"]
        for model_info in model_info_list:
            outputs_shape = model_info["op_output_info"].get(outputs, "")
            output_info_list.append(outputs_shape)
        final_op_info.append(output_info_list)

    final_op_info.append([SEPARATING_LINE] * (len(model_info_list) + 1))

    all_ops = {op_type for model_info in model_info_list for op_type in model_info.get("op_type_counts", {})}
    sorted_ops = sorted(all_ops)
    for op in sorted_ops:
        op_info_list = [op]
        float_number = model_info_list[0]["op_type_counts"].get(op, 0)
        op_info_list.append(float_number)
        for model_info in model_info_list[1:]:
            slimmed_number = model_info["op_type_counts"].get(op, 0)
            if float_number > slimmed_number:
                slimmed_number = GREEN + str(slimmed_number) + WHITE
            op_info_list.append(slimmed_number)

        final_op_info.append(op_info_list)
    final_op_info.extend(
        (
            [SEPARATING_LINE] * (len(model_info_list) + 1),
            ["Model Size"] + [format_bytes(model_info["model_size"]) for model_info in model_info_list],
        )
    )
    if elapsed_time:
        final_op_info.extend(
            (
                [SEPARATING_LINE] * (len(model_info_list) + 1),
                ["Elapsed Time"] + [f"{elapsed_time:.2f} s"],
            )
        )
    lines = tabulate(
        final_op_info,
        headers=[],
        tablefmt="pretty",
        maxcolwidths=[None] + [40] * len(model_info_list),
    ).split("\n")
    if elapsed_time:
        time_row = lines[-2].split("|")
        time_row[-3] = (
            time_row[-2][: len(time_row[-2]) // 2 + 1] + time_row[-3] + time_row[-2][len(time_row[-2]) // 2 :]
        )
        time_row.pop(-2)
        lines[-2] = "|".join(time_row)
    output = "\n".join([line if line != "| \x01 |" else lines[0] for line in lines])

    print(output)


def dump_model_info_to_disk(model_name: str, model_info: Dict):
    """Writes model information to a CSV file for a given model name and dictionary of model info."""
    import csv
    import os

    filename_without_extension, _ = os.path.splitext(os.path.basename(model_name))
    csv_file_path = f"{filename_without_extension}_model_info.csv"
    with open(csv_file_path, "a", newline="") as csvfile:  # Use 'a' for append mode
        fieldnames = ["NodeName", "OpType", "OutputDtype", "OutputShape"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        # If the file is empty, write the header
        if csvfile.tell() == 0:
            writer.writeheader()

        # Write the data
        for node_name, info in model_info["op_info"].items():
            op_type, output_info_list = info
            # Write the first row with actual NodeName and OpType
            row_data_first = {
                "NodeName": node_name,
                "OpType": op_type,
                "OutputDtype": output_info_list[0][0],  # First entry in the list
                "OutputShape": output_info_list[0][1],  # First entry in the list
            }
            writer.writerow(row_data_first)

            # Write subsequent rows with empty strings for NodeName and OpType
            for output_dtype, output_shape in output_info_list[1:]:
                row_data_empty = {
                    "NodeName": "",
                    "OpType": "",
                    "OutputDtype": output_dtype,
                    "OutputShape": output_shape,
                }
                writer.writerow(row_data_empty)
    print(f"Model info written to {csv_file_path}")


def get_opset(model: onnx.ModelProto) -> int:
    try:
        for importer in model.opset_import:
            if importer.domain in {"", "ai.onnx"}:
                return importer.version

        return None
    except Exception:
        return None


def summarize_model(model: onnx.ModelProto) -> Dict:
    logger.debug("Start summarizing model.")
    model_info = {}

    model_size = model.ByteSize()
    model_info["model_size"] = model_size

    op_info = {}
    op_type_counts = defaultdict(int)

    def get_tensor_dtype_shape(tensor):
        """Extract the data type and shape of an ONNX tensor."""
        type_str = onnx.mapping.TENSOR_TYPE_TO_NP_TYPE.get(tensor.type.tensor_type.elem_type, "Unknown")
        shape = None
        if tensor.type.tensor_type.HasField("shape"):
            shape = []
            for dim in tensor.type.tensor_type.shape.dim:
                if dim.HasField("dim_param"):
                    shape.append(dim.dim_param)
                elif dim.HasField("dim_value"):
                    shape.append(dim.dim_value)
                else:
                    shape.append(None)

        return (type_str, shape)

    def get_shape(inputs: onnx.ModelProto) -> Dict[str, List[int]]:
        op_shape_info = {}
        for input in inputs:
            type_str, shape = get_tensor_dtype_shape(input)
            if shape:
                op_shape_info[input.name] = f"{str(type_str)}: {tuple(shape)}"
            else:
                op_shape_info[input.name] = f"{str(type_str)}: None"

        return op_shape_info

    value_info_dict = {value_info.name: value_info for value_info in model.graph.value_info}

    def get_graph_node_info(graph: onnx.GraphProto) -> Dict[str, List[str]]:
        for node in graph.node:
            op_type = node.op_type
            op_type_counts[op_type] += 1
            for output in node.output:
                shapes = []
                if output in value_info_dict:
                    tensor = value_info_dict[output]
                    type_str, shape = get_tensor_dtype_shape(tensor)
                    shapes.append([type_str, shape])

            op_info[node.name] = [node.op_type, shapes]

            for attr in node.attribute:
                ATTR_TYPE_MAPPING = {v: k for k, v in onnx.AttributeProto.AttributeType.items()}
                if attr.type in ATTR_TYPE_MAPPING:
                    attr_str = ATTR_TYPE_MAPPING[attr.type]
                    if attr_str == "GRAPH":
                        get_graph_node_info(attr.g)

    get_graph_node_info(model.graph)

    model_info["op_set"] = str(get_opset(model))
    model_info["op_info"] = op_info
    model_info["op_type_counts"] = op_type_counts

    model_info["op_input_info"] = get_shape(model.graph.input)
    model_info["op_output_info"] = get_shape(model.graph.output)

    logger.debug("Finish summarizing model.")
    return model_info


def model_save_as_external_data(model: onnx.ModelProto, model_path: str):
    """Save an ONNX model with tensor data as an external file."""
    location = f"{os.path.basename(model_path)}.data"
    if os.path.exists(location):
        os.remove(location)
    onnx.save(
        model,
        model_path,
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location=location,
    )


def check_onnx(model: onnx.ModelProto, model_check_inputs=None):
    """Validates an ONNX model by generating input data and performing inference to check outputs."""
    input_data_dict = gen_onnxruntime_input_data(model, model_check_inputs)
    raw_onnx_output, model = onnxruntime_inference(model, input_data_dict)

    return input_data_dict, raw_onnx_output, model


def check_point(model: onnx.ModelProto):
    """Imports an ONNX model checkpoint into a Graphsurgeon graph representation."""
    return gs.import_onnx(model)


def is_converged(model: onnx.ModelProto, graph_ckpt, iter: int) -> bool:
    logger.debug(f"optimization iter: {iter}")
    graph = gs.import_onnx(model)
    if graph == graph_ckpt:
        print(f"converged at iter: {iter}")
        return None
    else:
        graph_ckpt = graph
        return False


def save(model: onnx.ModelProto, model_path: str, model_check: bool = False):
    """Save an ONNX model to a specified path, with optional model checking for validity."""
    if model_check:
        try:
            checker.check_model(model)
        except ValueError:
            logger.warning("Model too large and cannot be checked.")

    if model_path:  # model larger than 2GB can be saved, but compiler like trtexec won't parse it
        if model.ByteSize() <= checker.MAXIMUM_PROTOBUF:
            onnx.save(model, model_path)
        else:
            import os

            location = f"{os.path.basename(model_path)}.data"
            if os.path.exists(location):
                os.remove(location)
            onnx.save(
                model,
                model_path,
                save_as_external_data=True,
                all_tensors_to_one_file=True,
                location=location,
            )
            logger.debug("Model too large and saved as external data automatically.")


def check_result(raw_onnx_output, slimmed_onnx_output):
    """Verify the consistency of outputs between the raw and slimmed ONNX models, logging warnings if discrepancies are
    detected.
    """
    if set(raw_onnx_output.keys()) != set(slimmed_onnx_output.keys()):
        logger.warning("Model output mismatch after slimming.")
        logger.warning(f"Raw model output keys: {raw_onnx_output.keys()}")
        logger.warning(f"Slimmed model output keys: {slimmed_onnx_output.keys()}")
        logger.warning("Please check the model carefully.")
        return
    else:
        for key in raw_onnx_output.keys():
            if not np.allclose(
                raw_onnx_output[key],
                slimmed_onnx_output[key],
                rtol=1e-03,
                atol=1e-04,
                equal_nan=True,
            ):
                logger.warning("Model output mismatch after slimming.")
                logger.warning("Please check the model carefully.")
                return


data_type_sizes = {
    onnx.TensorProto.FLOAT: 4,
    onnx.TensorProto.DOUBLE: 8,
    onnx.TensorProto.INT32: 4,
    onnx.TensorProto.INT64: 8,
    onnx.TensorProto.UINT8: 1,
    onnx.TensorProto.INT8: 1,
    onnx.TensorProto.UINT16: 2,
    onnx.TensorProto.INT16: 2,
    onnx.TensorProto.BOOL: 1,
}


def calculate_tensor_size(tensor):
    shape = tensor.dims
    num_elements = np.prod(shape) if shape else 0
    element_size = data_type_sizes.get(tensor.data_type, 0)
    return num_elements * element_size


def get_model_size_and_initializer_size(model):
    initializer_size = 0
    for tensor in model.graph.initializer:
        tensor_size = calculate_tensor_size(tensor)
        initializer_size += tensor_size

    print("model size", model.ByteSize())
    print("initializer size", initializer_size)


def get_model_subgraph_size(model):
    graph = model.graph
    for node in graph.node:
        for attr in node.attribute:
            ATTR_TYPE_MAPPING = {v: k for k, v in onnx.AttributeProto.AttributeType.items()}
            if attr.type in ATTR_TYPE_MAPPING:
                attr_str = ATTR_TYPE_MAPPING[attr.type]
                if attr_str == "GRAPH":
                    print("subgraph", attr.g.ByteSize())
