# -*- coding: utf-8 -*-
"""执行列表源材料：@ 后面必须是分支，不能再用 commit SHA。"""
from app.modules.pipeline.source_ref_service import checkout_branch_of


def test_checkout_branch_reads_git_checkout_step() -> None:
    yaml_text = """
pipeline:
  stages:
    - name: 拉取
      jobs:
        - name: src
          steps:
            - plugin: git-checkout
              with:
                repo: maven
                branch: develop
"""
    assert checkout_branch_of(yaml_text) == "develop"


def test_checkout_branch_falls_back_to_ref() -> None:
    yaml_text = """
pipeline:
  stages:
    - jobs:
        - steps:
            - plugin: git-checkout
              with:
                ref: release/1.0
"""
    assert checkout_branch_of(yaml_text) == "release/1.0"


def test_checkout_branch_defaults_master() -> None:
    assert checkout_branch_of("") == "master"
    assert checkout_branch_of("pipeline: {}") == "master"
