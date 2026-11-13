# Changelog

## 1.0.0 (2026-11-13)


### Features

* add apply command for settings, topics, descriptions and labels ([fe0b878](https://github.com/JonathanTSilva/jts-ops/commit/fe0b878dd515ff9f02f881e220b149832203761c))
* add audit command ([2a1cbdb](https://github.com/JonathanTSilva/jts-ops/commit/2a1cbdbd7bb44d7d612451612c51bde49cef1505))
* add gh adapter and repo state ([7b79565](https://github.com/JonathanTSilva/jts-ops/commit/7b79565c87a777e89901b3c1f7828aa609cbb425))
* add migrate and adopt commands ([#4](https://github.com/JonathanTSilva/jts-ops/issues/4)) ([81e90b3](https://github.com/JonathanTSilva/jts-ops/commit/81e90b30468ed54f3c01774b0af064241d94c751))
* add new command to create compliant repos ([662d395](https://github.com/JonathanTSilva/jts-ops/commit/662d3954bda6c1c8f69bbd48247a34624b885288))
* add sync command ([#17](https://github.com/JonathanTSilva/jts-ops/issues/17)) ([be508b1](https://github.com/JonathanTSilva/jts-ops/commit/be508b16124a0d4b988916db89fe8aea69cd5aa0))
* add systemd timer for sync ([#19](https://github.com/JonathanTSilva/jts-ops/issues/19)) ([29cf601](https://github.com/JonathanTSilva/jts-ops/commit/29cf601f5de603fbc0abadcc15299ca181f1764b))
* audit visibility against the inventory ([#15](https://github.com/JonathanTSilva/jts-ops/issues/15)) ([b2bc235](https://github.com/JonathanTSilva/jts-ops/commit/b2bc235bc965ab0e007c33c8027d14edec0f330f))
* brand command generating the README header ([#46](https://github.com/JonathanTSilva/jts-ops/issues/46)) ([#47](https://github.com/JonathanTSilva/jts-ops/issues/47)) ([f944799](https://github.com/JonathanTSilva/jts-ops/commit/f944799b9679644c07d65d4b98d7c6d0445f9d78))
* expect .claude/settings.json in the active profile ([ab33441](https://github.com/JonathanTSilva/jts-ops/commit/ab33441047113dde9625fb0d737a96ae39b06571))
* jts-ops ideas turns vault idea notes into issues; vault-sync dot-notes and heading fix ([5bd153e](https://github.com/JonathanTSilva/jts-ops/commit/5bd153e4616f2dc10fe6b1a36375e379459bf492)), closes [#32](https://github.com/JonathanTSilva/jts-ops/issues/32)
* jts-ops ideas turns vault idea notes into issues; vault-sync fixes ([#33](https://github.com/JonathanTSilva/jts-ops/issues/33)) ([5bd153e](https://github.com/JonathanTSilva/jts-ops/commit/5bd153e4616f2dc10fe6b1a36375e379459bf492))
* jts-ops project ensure and sync for the Engineering project ([#36](https://github.com/JonathanTSilva/jts-ops/issues/36)) ([3aef258](https://github.com/JonathanTSilva/jts-ops/commit/3aef258778117ec19cd8031d1ef57e82261c8c25))
* jts-ops project ensure and sync for the Engineering project ([#37](https://github.com/JonathanTSilva/jts-ops/issues/37)) ([3aef258](https://github.com/JonathanTSilva/jts-ops/commit/3aef258778117ec19cd8031d1ef57e82261c8c25))
* jts-ops vault-sync writes a repo note per repository into the vault ([#31](https://github.com/JonathanTSilva/jts-ops/issues/31)) ([748ed72](https://github.com/JonathanTSilva/jts-ops/commit/748ed72ae35debbc633e3954988ce6b204ff2ac3)), closes [#30](https://github.com/JonathanTSilva/jts-ops/issues/30)
* load standard and inventory, validate repo names ([f680b07](https://github.com/JonathanTSilva/jts-ops/commit/f680b07468703d7296ac8b6a94e5f418d2e773d2))
* nightly public audit and dashboard data on Pages ([#21](https://github.com/JonathanTSilva/jts-ops/issues/21)) ([82e0851](https://github.com/JonathanTSilva/jts-ops/commit/82e085122d5a9afa24e8c86c4bdc2a6c2b7e2acf)), closes [#20](https://github.com/JonathanTSilva/jts-ops/issues/20)
* pin template v1.2.0 and offer the new stacks ([#27](https://github.com/JonathanTSilva/jts-ops/issues/27)) ([b01def5](https://github.com/JonathanTSilva/jts-ops/commit/b01def5c27410066da181a427f291896e7c102fb)), closes [#26](https://github.com/JonathanTSilva/jts-ops/issues/26)
* schedule vault-sync and project sync ([#41](https://github.com/JonathanTSilva/jts-ops/issues/41)) ([b90e877](https://github.com/JonathanTSilva/jts-ops/commit/b90e877ad4dd8fea37ee77aa9e3441073f44e1f2))
* schedule vault-sync and project sync in the sync service ([b90e877](https://github.com/JonathanTSilva/jts-ops/commit/b90e877ad4dd8fea37ee77aa9e3441073f44e1f2)), closes [#40](https://github.com/JonathanTSilva/jts-ops/issues/40)


### Bug Fixes

* audit crashes on repos without merge settings ([#25](https://github.com/JonathanTSilva/jts-ops/issues/25)) ([1ae30e4](https://github.com/JonathanTSilva/jts-ops/commit/1ae30e4da66095c8772b89df2cd10026d325c9f2)), closes [#24](https://github.com/JonathanTSilva/jts-ops/issues/24)
* audit workflow uses runner context in job env ([#23](https://github.com/JonathanTSilva/jts-ops/issues/23)) ([b80a94b](https://github.com/JonathanTSilva/jts-ops/commit/b80a94bd71a2b9514384e29f850f4fa89778629d)), closes [#22](https://github.com/JonathanTSilva/jts-ops/issues/22)
* **audit:** skip ci.failing for archived and copier answers for templates ([#13](https://github.com/JonathanTSilva/jts-ops/issues/13)) ([714db53](https://github.com/JonathanTSilva/jts-ops/commit/714db53b75e04f048f4716fd5027fbbec5f7e2bb)), closes [#12](https://github.com/JonathanTSilva/jts-ops/issues/12)
* **audit:** take the CI signal from the latest completed ci.yml run ([#11](https://github.com/JonathanTSilva/jts-ops/issues/11)) ([838d2fc](https://github.com/JonathanTSilva/jts-ops/commit/838d2fc4713000d2d35fa80ac67edbf746dd61fe)), closes [#10](https://github.com/JonathanTSilva/jts-ops/issues/10)
* harden apply settings and execution ([0ec8fd3](https://github.com/JonathanTSilva/jts-ops/commit/0ec8fd3517ce2bcbf5a8550c0c52ea03dcde2208))
* ideas scans what is sent and refuses a target GitHub does not list ([#35](https://github.com/JonathanTSilva/jts-ops/issues/35)) ([90bdabf](https://github.com/JonathanTSilva/jts-ops/commit/90bdabfffeb58083496fec104554eea268815d03)), closes [#34](https://github.com/JonathanTSilva/jts-ops/issues/34)
* match brand markers only as whole lines outside code fences ([#54](https://github.com/JonathanTSilva/jts-ops/issues/54)) ([ef0d6e6](https://github.com/JonathanTSilva/jts-ops/commit/ef0d6e627797c806a629a10b4037804a8a5929af))
* match brand markers only as whole lines outside code fences ([#55](https://github.com/JonathanTSilva/jts-ops/issues/55)) ([ef0d6e6](https://github.com/JonathanTSilva/jts-ops/commit/ef0d6e627797c806a629a10b4037804a8a5929af))
* match repo names with fullmatch ([692a2b9](https://github.com/JonathanTSilva/jts-ops/commit/692a2b9f51199f34395f27f919c86d20f06e8c0c))
* project ensure updates the built-in Status instead of creating it ([#44](https://github.com/JonathanTSilva/jts-ops/issues/44)) ([aa47202](https://github.com/JonathanTSilva/jts-ops/commit/aa47202a296d35477b59eb76172d1ffc05560826))
* project ensure updates the built-in Status instead of creating it ([#45](https://github.com/JonathanTSilva/jts-ops/issues/45)) ([aa47202](https://github.com/JonathanTSilva/jts-ops/commit/aa47202a296d35477b59eb76172d1ffc05560826))
* **security:** allow only exact git read commands for agents ([6f64758](https://github.com/JonathanTSilva/jts-ops/commit/6f64758fc1f1289d4ee5134bfd439f5ac300372d))
* **security:** deny git output and ext-diff escapes in agent allowlist ([866fd15](https://github.com/JonathanTSilva/jts-ops/commit/866fd15fac93ec7900162b352d2c5630f4d01103))
* strip control characters from GitHub text before printing ([#38](https://github.com/JonathanTSilva/jts-ops/issues/38)) ([cd32c6c](https://github.com/JonathanTSilva/jts-ops/commit/cd32c6c9b0a66d833f7f7e18a266dbdf5082c048))
* strip control characters from GitHub text before printing ([#39](https://github.com/JonathanTSilva/jts-ops/issues/39)) ([cd32c6c](https://github.com/JonathanTSilva/jts-ops/commit/cd32c6c9b0a66d833f7f7e18a266dbdf5082c048))
* sync only treats unpushed commits on the default branch as dirty ([#43](https://github.com/JonathanTSilva/jts-ops/issues/43)) ([b26a0af](https://github.com/JonathanTSilva/jts-ops/commit/b26a0afee1883f93bdbf5bfa6fdb5604d497c3c9)), closes [#42](https://github.com/JonathanTSilva/jts-ops/issues/42)
