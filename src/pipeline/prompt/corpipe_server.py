"""CorPipe kept loaded: the model is loaded once, then each prompt file is processed as `corpipe26_onestage.py --load
MODEL --exp . --epoch 0 --test FILE` would (same code, same options), without reloading the model (~15 s each).

Runs in CorPipe's own environment (.venv-corpipe), started by coref.mentions:
    .venv-corpipe/bin/python v2/prompt_ast/corpipe_server.py MODEL       (cwd: the work folder)
Protocol, one line each: a .conllu file name in → "@@corpipe ok" (its mentions are in NAME.00.conllu) or
"@@corpipe error …" out. Everything else CorPipe prints goes to stderr. Ends when its input closes.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".venv-corpipe" / "src" / "crac2026-corpipe"))
reply, sys.stdout = sys.stdout, sys.stderr                    # the protocol owns the real stdout

import huggingface_hub  # noqa: E402
import torch  # noqa: E402
import transformers  # noqa: E402
import corpipe26_onestage as cp  # noqa: E402


def load(model: str):
    """corpipe26_onestage.main, up to the loaded model (inference only)."""
    params = ["--load", model, "--exp", ".", "--epoch", "0"]
    args = cp.parser.parse_args(params)
    cp.minnt.startup(args.seed, args.threads)
    path = model if os.path.exists(model) else huggingface_hub.snapshot_download(model)
    with open(os.path.join(path, "options.json")) as f:
        args = argparse.Namespace(**{k: v for k, v in json.load(f).items()
                                     if k in ["batch_size", "depth", "encoder", "right", "segment", "treebanks"]})
    args = cp.parser.parse_args(params, namespace=args)
    args.load, args.logdir = [path], "."
    name = ("google/t5gemma-l-l-ul2" if "t5gemma" in args.encoder else "google/umt5-xl" if "umt5" in args.encoder
            else "google/mt5-xl" if "mt5" in args.encoder else args.encoder)
    tokenizer = transformers.AutoTokenizer.from_pretrained(name, legacy=False)
    tokenizer.add_special_tokens({"additional_special_tokens": [cp.Dataset.TOKEN_EMPTY] +
                                  ([cp.Dataset.TOKEN_CLS] if tokenizer.cls_token_id is None else [])})
    tags = open(os.path.join(path, "tags.txt")).read().splitlines()
    zdeprels = open(os.path.join(path, "zdeprels.txt")).read().splitlines()
    m = cp.Model(tokenizer, tags, zdeprels, args)
    m.load_weights(os.path.join(path, "model.pt"))
    return args, tokenizer, m, {t: i for i, t in enumerate(tags)}, {z: i for i, z in enumerate(zdeprels)}


def main():
    args, tokenizer, model, tags_map, zdeprels_map = load(sys.argv[1])
    for line in sys.stdin:
        try:
            test = cp.Dataset(line.strip(), tokenizer)
            loader = torch.utils.data.DataLoader(test.dataset(tags_map, zdeprels_map, False, args),
                                                 batch_size=args.batch_size, collate_fn=cp.Dataset.padded_batch(False))
            model.process(args.epochs, [(test, loader)], evaluate=False)
            print("@@corpipe ok", file=reply, flush=True)
        except Exception as e:                                # one bad file: answered, the server stays up
            print(f"@@corpipe error {e!r}".replace("\n", " "), file=reply, flush=True)


if __name__ == "__main__":
    main()
