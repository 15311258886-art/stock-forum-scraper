from .kronos import KronosTokenizer, Kronos, KronosPredictor

model_dict = {
    'kronos_tokenizer': KronosTokenizer,
    'kronos': Kronos,
    'kronos_predictor': KronosPredictor,
}


def get_model_class(model_name):
    if model_name in model_dict:
        return model_dict[model_name]
    raise NotImplementedError(f"Model {model_name} not found")
