# Permanent reference runs

Runs kept for byte-for-byte non-regression. They live outside every git
repository, in `reference-runs/` at the root of the work tree
(`/home/yann/mesh-emulator-rework/reference-runs/`, ignored by the root
repository's `.gitignore`); this file is the committed copy of
`reference-runs/README.md`. Everything else is reproducible from its commits,
seed and options: virtual-time runs are deterministic.

## dag_simple_8c5677e (current)

| | |
| --- | --- |
| What | FAN 1.0 `dag_simple` (15 nodes, no LFN), seeds 1–10, 300 virtual seconds each, virtual time, no clock drift, legacy medium (`--medium=legacy`), no TUN device (`TUN=none`), `SETARCH=1` |
| Stack | `nanostack-generic-standalone` 8c5677e (GS12: a MAC retry after a missing ACK waits for the FHSS hold, then a random backoff in IEEE 802.15.4 SUN units; the stack's `docs/MAC_RETRY.md`) |
| Emulator | `mesh-emulator-orchestrator` ac0b007 (in-tree `build-vt`) |
| Made with | `sim_utils/migration/nonreg_dag_simple.sh build-vt $(pwd) none <out>` (emulator branch `gs12`: reference `none` makes the runs without comparing them; the options are those of every comparison) |
| Size | 274 MB, 783 files (`seed1`–`seed10`, `seed<N>.run` the runs' outputs, `results.txt`, `SHA256SUMS`) |
| Compare | `sim_utils/migration/nonreg_dag_simple.sh build-vt $(pwd) /home/yann/mesh-emulator-rework/reference-runs/dag_simple_8c5677e`, as for the previous runs below; exit status 1 on any difference |
| Regenerate | check out the two commits above, build `build-vt` (`docs/VIRTUAL_TIME.md` §2), then the command above; the logs must match the checksums below |

Why new runs: GS12 changes the timing of every MAC retry of an FFN, so the
FAN 1.0 logs differ from the previous reference from the first retry on (all
10 seeds). A build of stack 8c5677e with `-DNANOSTACK_MAC_RETRY_SUN_BACKOFF=OFF`
still gives logs identical to `dag_simple_b1b9cc5` (10 of 10 seeds), so the
change is the retry backoff alone.

**Statistically equivalent to `dag_simple_b1b9cc5`** on the join milestones
(10 seeds each, every router pooled, first 295 s, Mann-Whitney U test):

| Milestone | Routers that reached it (old / new) | Median, s (old / new) | p |
| --- | --- | --- | --- |
| parent selected | 138 / 135 | 92.2 / 94.0 | 0.90 |
| authentication done | 136 / 135 | 96.7 / 97.8 | 0.75 |
| operational | 135 / 132 | 103.2 / 106.0 | 0.69 |
| ready (`ARM_NWK_BOOTSTRAP_READY`) | 129 / 127 | 146.9 / 149.5 | 0.57 |

Routers ready by 295 s per seed: old 12–14 (median 13), new 12–14 (median
13). `sim_utils/migration/analyze.py` gives the per-node tables and the
message-type histograms: the frequent message types differ by at most 7 %
in count per run; 5 rare ones (under one per run) appear in one group only.

### Checksums

`SHA256SUMS` in the run's directory lists every file (sha256 of the list
itself: `7eae876e95cd6d77beacf5f69347b40e5ec9de7b046c7caf261802d0d344ffb1`; `sha256sum -c SHA256SUMS` from that directory). The files
`compare_logs.py` reads:

```
5cc190b3e61373c608296c08425f0eca905581bc273ec2b376de75b8f2338969  ./seed1/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed1/exit_codes.txt
55ac436dc57cea6a5f727ccb61dda9f899e818a639ad5b0b999494a3039f4da2  ./seed1/log_wsbr
2574aafe877d7c4b2b7fdcd9026afb967604925e2111a207305846f7707d811b  ./seed1/node1.log
d3c3373472e0a6bc91a0db8471c75ddeeb058910d90c530d60d2d8838d7fa92f  ./seed1/node2.log
0e1df4cd53ad7af9ace5ec16c547d88f66918ba4d59735b1aa84b9222f1ca41c  ./seed1/node3.log
fb062f14bd07106685538d2ddca11aaa95d2b6ed91eb87571741bf7973f3830c  ./seed1/node4.log
7d8e6aeaee51b66a7b5d307f63bae7ab4e323358a42a2559024b6370dfa0783a  ./seed1/node5.log
35d99e10cba12b26ad97c3438222ccd43f45e43eb49435102a9dc4dc86611228  ./seed1/node6.log
e7dd51c3e29ee87c59ec0decba9d3510f37777f72d17499a25001e2e1e28e8da  ./seed1/node7.log
9350fb901c31e945239123e2cb68a79684e49e1fb370af1a746f5f5e22a6eb92  ./seed1/node8.log
22aeea9f0e09a30d99ebe9682a825a63ba96d239668ea04b6058000484378459  ./seed1/node9.log
ed470c3b385ff6b116e464c9d3a1d29c153a7ae6e519618d4cd344ae715bab5a  ./seed1/node10.log
16d449adb65e4067e42000cf83291d49bfabf3d73148ecf04dd7d0ab5eacc058  ./seed1/node11.log
4986cb43fdca748f64a131a9ace9745676bc57319f7347db0f9fd5405776d858  ./seed1/node12.log
01251cac9a42eb6d2595c2f4d7365c90cca74ce5de8e4f052623641fe0cb36d9  ./seed1/node13.log
85a44cc30e352fda7ee3d170cd60c2e395624819372fa360d4df3eec05b87058  ./seed1/node14.log
3511c0540babcab9466d72bac3d67e9a97e73150e8d1591bd3b0c9262e69a319  ./seed2/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed2/exit_codes.txt
bb2be27f09d4b36629a2457d6523277d9224725b6a188b88bc59ff4d3798b1ba  ./seed2/log_wsbr
bcdde6c5a26872458255dab7f1d5e34cb577f7f15b4e178284d5124692d20485  ./seed2/node1.log
eb1b7d4aa903ee74fc4a747527b1fe349225b6ce35340b895d5aa32adb3011ce  ./seed2/node2.log
40067f9179605eaeb4cdaba4467e24ad567ca3695c93c0e030be37a9ede4c09a  ./seed2/node3.log
2c658e777cbbc4331a72fda1dd258b49116f78bb9193f92b1cc5913f8c38b3df  ./seed2/node4.log
852a40f72d449d70e9e74a70a7d943ac79d691ab1dc66a3f60ef19e5cb480c48  ./seed2/node5.log
784a2c27dca80e02928bf46221aa091ca0b0356f98927904a30a302006f1212f  ./seed2/node6.log
2f5793ccae8d731185c8c389c80ab43966c6bac59dce02535fc96c4471d61aeb  ./seed2/node7.log
83f70ad2f1250639c737e2064e0ea629bfd053eb615383ed8e5198e6bf5a2092  ./seed2/node8.log
08350f24f0c18621aa0aedfe4547e6eb3cef53681663f0db82189df4f452b746  ./seed2/node9.log
b870fbd7eb99485249d99fc490d9e8f05291561d6d2e4c1492cef833e6f7b4f3  ./seed2/node10.log
960953889c6aad2054ec17b8bbe462b53c2d637519d1c5df0beb83609e7e7df2  ./seed2/node11.log
ae0369242102945c9f48579377d8c4c436f59106231647c6c0d30adaf74a6923  ./seed2/node12.log
6d3e94fa79e5c4d71bc5f1173ea31cc25f241511a76a57e15ca0f88d8440e466  ./seed2/node13.log
f542abaf8258102e5d851f3c8ab2589d773ee9d51b6675f2c3d77dd2afef7082  ./seed2/node14.log
8b17a4cac02634b446b32f53687222dc6bef5650fbe893d96e17ed4d978e648c  ./seed3/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed3/exit_codes.txt
d49b893427dcec42733c2d1138bb132bb18548acb478d82839a44fb6c13a914a  ./seed3/log_wsbr
6134b3dd7505b37ccf888a451fe7ca66d688027e7ae5f14dfe90372cc6c7b8f3  ./seed3/node1.log
434c1b59321d561ea91580f4091b818d232b3795e82ad14d5c3ae82f5ea2b935  ./seed3/node2.log
232285df01c53ade73352b2655967b3eff3a513c4715d3e7e1559527a4ff6628  ./seed3/node3.log
867d24b7471d1a40701192a0108107354888bdfff87251f4f5a549d3495c84d3  ./seed3/node4.log
cee09967eddfda466ee8ed38942001f98e12cf069f4993c376b1bd611279131e  ./seed3/node5.log
aba7250f71b960a6e6195492901d501a168e19ad7cfcf742bb4a688a4a163496  ./seed3/node6.log
ae071460ec32c0b3071f9635276a54ad1b5186972f43a0ee0df522130d31346c  ./seed3/node7.log
5e3677be33dba0819e46ec11aad054a6949fc62aa5cf7b31acedb0334e98c2ee  ./seed3/node8.log
2c0586887d957ff62e3f8de9698676b9c712bf3aa58d8a9497c7c585f32a1c3b  ./seed3/node9.log
57b1dfa1096f72d893932965d9d2d496ef53200e760f8bab112623bf1f02cace  ./seed3/node10.log
4ccbef295bb7d0442a66d1812b1d75b1f5a058909915c2226b355629a2141c0b  ./seed3/node11.log
4eb5f78d8922b8114938fa8d5b19b58593dcc9c2abef421eef36f535cf4f249f  ./seed3/node12.log
d0e524c6ecba8ff897e0f552b2ec8bc889f32e4c4dee9d4e72fd5feb8b68af21  ./seed3/node13.log
eb22ae0efdaee407f3f671709aec8d81dc1eb691f4dcb1dcf56e45f5f40faa9f  ./seed3/node14.log
be0423284699ef2639144d35d6a67bb53ce1bd22dd6eea73e87d0ce7ebce7587  ./seed4/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed4/exit_codes.txt
c258848942b3d6e4d9a6af03393fbd9edf8a1f8dd04628078b44c0fba248081a  ./seed4/log_wsbr
7acbf4b7e7663f09a9543abd34bfcc965eb3397d8b65cc140f7c53b3f5d31ec9  ./seed4/node1.log
a6753e6df3b377a52d53f4d72b75189cd1533da6d99cac91d431e73e4340eac5  ./seed4/node2.log
71ea5dcfb7018d21b9fffa1a904183898edc36a640a3d77bcf874c25601889a0  ./seed4/node3.log
dbdbd86318aba709d0d5c616875b220a520ba0232093ca333c29c6622ac3a8fe  ./seed4/node4.log
29597a0f3d962d49b061d69d75afc1ea56db9f21dcb26b91bf976f1106754a09  ./seed4/node5.log
224101b0b3d0f122b784e49125e697d353a0701d02d0dacd5da128944481ae9e  ./seed4/node6.log
b78797f124dd82c517df916664e29d66b93b59f1257661a8a03b2e66feffb592  ./seed4/node7.log
6f6f077879a5369af674644286871f3f849f8ed64d07d70a7c98ffed1677fa81  ./seed4/node8.log
d507f69ceeeb99da4bb6ff540c5909d1e8e69456c966606d9defafc432126b67  ./seed4/node9.log
49cd2d2e5887da9a03d35a9c5f596f8ede2aa4f7572e353d1d3705e39338cfbb  ./seed4/node10.log
551cf87dd136b977360033b430844f6815b705fdbb936fe5089f98d9ea23ba59  ./seed4/node11.log
ed57c14a07af85d4820cf90da4a53f69dd3893dd383d9e5fb387b61667fe6a7b  ./seed4/node12.log
c9a345f3901ace5ee8bce141da883c94fb4808a831603a4f4a9fdb58707dfc20  ./seed4/node13.log
f4a90e3b3533572f82fc035272920b71aea2e4a9311afda7c95f5b8c8b2c1de5  ./seed4/node14.log
3aa923d9b2b504d86c5b711598ef754658834a541b3a4d05120d802be1193ba5  ./seed5/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed5/exit_codes.txt
6cc84be9f85bb19419aede19bcd179338752a0f3f8a8bc90c3dc6a64315f142a  ./seed5/log_wsbr
1a784e3115e62575449a51f227ba8f46738d8c9b8e4317385dbda4c193275c40  ./seed5/node1.log
9996006e0253bcd31e22f7f02a81b9a45086eca628632dcaa8e844625afa7b2c  ./seed5/node2.log
5546e74f081eafe4d70731a59baffc428986f0658f75e4ecda6c74ca0135f01c  ./seed5/node3.log
912fde8bc100597e849d2d376352670986a9793c5a58eed6b7286063df0c7110  ./seed5/node4.log
89e85238f98a71264a296423f84649227b1894b20702f8eeecabe5ba1f9eda03  ./seed5/node5.log
ef850e9c312e1cb1f77812fd31d8f22397b99f8750b8cd4a633d96a0b932be3b  ./seed5/node6.log
7c55723658c1221f54af18a2c35e17412d5b8183ec1bc4e63ffdcd3c90154dc8  ./seed5/node7.log
1b87ba45f305400128b5ee44ad0e29c4b105666db13a7bf04676154f3e7333cb  ./seed5/node8.log
f0202506b32bbc6b52e978b8ff1e7a38f2b47bae84da881529286fd211bb0929  ./seed5/node9.log
9afbe6d4a86d6b3c26b386c298ceeed540bcbca8893cdded1bab85f9921bfcd9  ./seed5/node10.log
21b7feab8addd6ea1d576628379061582d7e0de7200920f6eb991efd01613d31  ./seed5/node11.log
8663c2e16082bc74a230e25555e04fe385a9845192bb3367607ea6ab044d9ea7  ./seed5/node12.log
256c6b6e1d72da3415b93d1b45619ccddb2b9c71f5c742fb65096dcfbac4fbfd  ./seed5/node13.log
b5541d2469ea48b89a7e1fe6e1b0f96029c29a9e109bfe17d13fed0ea22f9454  ./seed5/node14.log
ceae947e11fca7ee2ed5cad58a4e3bae8321c8d223b0af7130724a5f45499263  ./seed6/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed6/exit_codes.txt
6924f5abf997450daf3c58017627416a41dc81ebc2c1514b042051692eec315e  ./seed6/log_wsbr
cc224fa3bccf3c7e26f0d84f57c59f0959508b9b53beb83b3ca1225156114642  ./seed6/node1.log
90812b96a62cce6ced55368ae4b5e6c93fbe41cfcf7ce67148eff652aea89860  ./seed6/node2.log
6444e71185eb0b0b4bda0b8b1d67a875a5f05fd3f3fdb7e2d9c7bf663c807ada  ./seed6/node3.log
615673239eb73461c5d30c397b33b69a239540ad83949765a32e3617106ae8f1  ./seed6/node4.log
20fb419523d386e93d7cc44b64438598c5b911bc5ab1695322c5a8ce9f0964e6  ./seed6/node5.log
bd36325641eab4b089cdc488ab2fb4e4d6238ab3a16efe0b55a5d169ac7c02bb  ./seed6/node6.log
181f0bb635564fb2ca827283bf2f649330b4022e7976e53f1b1a7adb41f2bd76  ./seed6/node7.log
dc41d9e3be1b87b11ff2146f1b6fadd70218c229bbd4f55ee0720b442faf1626  ./seed6/node8.log
2e420bf067374c85e3163dde6b6babe9549c5616096d7e56227dca7f91905fcd  ./seed6/node9.log
28bcea989132e708f7bfff6ad2cfcf8e659694b7bd055e1e6db03692cf2f90f5  ./seed6/node10.log
7f7c3ae86568641db991f36c42104723588abb56b10296c0e72151146b737ca7  ./seed6/node11.log
176023c318b7fd414aa29524e4a1b1998072524af9b9e38add2562aa613d59ff  ./seed6/node12.log
dd601ffebad1439f8431c3fc558bfd3576693839fde15139ce4cd00c15a6febd  ./seed6/node13.log
dbb303ab998ec616c96a99b310677ef20d6b24f41b1a6831313dd3de99e65c99  ./seed6/node14.log
35abc8146186179b3c336a6d2f0b5c404451a21ccd1f1834f3961ce39a4663a3  ./seed7/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed7/exit_codes.txt
931bc4e2511a9af757772c402ed06971560cf661e129c09dfcefb39314304588  ./seed7/log_wsbr
53cafd063a73930b52575af7a94f48bf50bccc7e2037f0aba52320d43cf3ac4b  ./seed7/node1.log
c836e555899138b5b2a4da1c411a2133e5e1797fc86d0f18b3bf4aaee2dff2a8  ./seed7/node2.log
a1df361c4af3a2ba8338760903e967df908d6188d061f1670908c38a626a0ac1  ./seed7/node3.log
41eed3138327ddb1209b4097e273692b04273d704b17a46361e85acf57c89f59  ./seed7/node4.log
dec170dd8a245e43d23dd3315f348810fb40a3a97d556412ccee6d46f74343e2  ./seed7/node5.log
2eeb1064bcb0b8057ccf0bc1165a9275eece90154e2457ffbdaf053c954f74ce  ./seed7/node6.log
94c15983610f9df1b0c9a0fee570c0291f4018a7dc3b82e40d2651ce64e1e411  ./seed7/node7.log
aee230358f5fe20fc8a20b9abaabb9c61b033de5a2ff154db36f54c5617a554c  ./seed7/node8.log
6a3edb5c456c112606da43d597c98c285124ff64ee2432ab9efa28a9eb429b84  ./seed7/node9.log
2ef0a30accb0dacd417e626ac06a79f6e8dc6ac7e5867236b5f1a01096248e42  ./seed7/node10.log
0d002905abb5b2f8ebbd8bb6a93d127b8d21c14891bd0ae2b2f4061765324bcf  ./seed7/node11.log
30b8f5b15cc60c7f34ec1d02083cd38b42a628b54c7a193271823fff9c732aa3  ./seed7/node12.log
3e38f422471eddf57085e07392452b3721b0652132af83972cd0cdba3442ee29  ./seed7/node13.log
61a43d013c767784ebf65623bb445ec262506ebe7c245e70037cb73d65934bb4  ./seed7/node14.log
4e89f27d0f686639155b555c790c86f38551e3e52a1ff7f7f3a5125cdda4aafe  ./seed8/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed8/exit_codes.txt
63d0867c9bc115d79c013a1244594048bcc12aaea73575034b5a03738f7605b8  ./seed8/log_wsbr
ec3de8ab697944728a78301f917f9f3e931ab13119ed280d6e7ad17ada281d16  ./seed8/node1.log
29352aa4545375a7cd36a70215f8cd72ab50253a4cd08537ec67424c9db34639  ./seed8/node2.log
8d5e701af5666a943d7cb2968e9e3048c11f4a8f63018d2553b54f220cecd6a6  ./seed8/node3.log
39b70c9c4f6a29031de5096030dd91c5c9840264b32a74982d10f071cd40a0c6  ./seed8/node4.log
9a440690c453991ccb6654fa6f39fcc13ab821dbbb418726608a5c21124edd51  ./seed8/node5.log
eee9fabe8e3768e6a29024a89fbf36a25ac5db3f398c337f6d8d8897d670606f  ./seed8/node6.log
94846857dce9dd9f8e16d867a0d3867b5a534002e3122e5536e78630b56d611a  ./seed8/node7.log
e1dfce143510deeb3232d7c91706992c6498da7de30e531f2b4c11c13e1b0a88  ./seed8/node8.log
1f9e563e3b792db25ecaa3c39562e11cd72856dc239f9890656e695ed11ce91c  ./seed8/node9.log
c21eaa95edead042fd35c67ad2fafb8d5c0ba584076760733c290160b4673385  ./seed8/node10.log
0dad881923b625ed589d2dbe1bc6472a5e98fc7509b68112138dd8cfb1e75d21  ./seed8/node11.log
b65812555869405f12bc30a5bf3b798e687cfeb15f64e2896ad00deee5023baf  ./seed8/node12.log
e20a0b9c71897668c3e1a5a827fc8863718510e2f47180e81ed116d41d6d7ae2  ./seed8/node13.log
e2a8e8387643ba6522291defd4ee2b05fd1ff8acf64478446b50c7be8eae99b9  ./seed8/node14.log
b62c7990ac434b379849095415abbac9f8470702356ceb5734d6d4a3d7ed8ca0  ./seed9/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed9/exit_codes.txt
2c3472676943a799f5cc5f3d5a02d38e185ae8c0d39ce7318a29f07b818776a0  ./seed9/log_wsbr
a9e2705a6ec8dffb6e42deb15e479b168e0bc13cc9b6319319aef271b656b083  ./seed9/node1.log
e888c7bffecb594197c3483cc832033a3c743ca93cb99025875f54dc11ba033d  ./seed9/node2.log
c2104b89b142f28f648fae13abf87da7b774a93a3f7ffe85ea973041ec055d93  ./seed9/node3.log
f04933e216f4ae0a609e85920b2dbd60ef6dd7a4b7ac77fc9b12f0f4e572f494  ./seed9/node4.log
b8e763d577cd256d6cebda828d0019beebf9239ab965704361aebb2f250e940e  ./seed9/node5.log
6caa14c9376856b169a7c445f5389bab1c2798e27de5e03e822e199ed7bf2021  ./seed9/node6.log
05e92945dd7c4886886bbae61453c29a9a6916ad0a295f323e9d073ecc0ed3f6  ./seed9/node7.log
3102d81106655dd57eae8fc0e64ce45631005725fda87aa41bdfb098db72242f  ./seed9/node8.log
69475ee49bae0ac3159d7544e567cabd77130301b86ac174c981df2655bc9bb1  ./seed9/node9.log
0c2b5e25dd1a093bfdc1633d38c2dfdf2d43378b0847757604f2ae849a325b38  ./seed9/node10.log
34df1f2df00ed19920688257bc2ad5cb4412d2579510338933579061fe98f091  ./seed9/node11.log
94ee03b781a664b68d8f85018a1615ed6137f20188f4fb737e5c4695d8f20d0e  ./seed9/node12.log
3bbe5ce2a94b13b381adb3622ecbfefcbe261dced4f55fd29f74a9ad0f2079d8  ./seed9/node13.log
eea24285942fee434a28e4c46834845464159fc9c3e735e52450aab0fbb25ff0  ./seed9/node14.log
63c60357c8065aae87ba5290b335d680924e522c7bbd49fdc4722afc9439c748  ./seed10/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed10/exit_codes.txt
2d50b01df68f135f6955c319f3ef6cf6854a6d0bee427817596f2e9f325cf59a  ./seed10/log_wsbr
f00b82cff9d344d637aeab9bfa6abfe9a154379426b0cfd8e579e07567510ff9  ./seed10/node1.log
27582fd4ee786b58d3b5595246710cc2ac8633fc84277bcac03b0a3b9e923e0e  ./seed10/node2.log
a4af16ddd8db756a7bbb7dada7d080fe578de0e8b1dfbdc43bd5ad8868ba389c  ./seed10/node3.log
6aa2058119a575964546e52a908f79140b87bc9cf8df9622e4c3ad9002de85bd  ./seed10/node4.log
c5b93140457cda626ee20bdc371db5dc6f9a34c4e1d07c6b9b4ac90fd5575454  ./seed10/node5.log
da454e569632b85d622b4978a422a362944a7afe389d9a49572c90513008b686  ./seed10/node6.log
6f736e1a4778555f59524463347f890f96d19aa4eec46fd5b337d89881066175  ./seed10/node7.log
cd7fe0297561d629d28bee282369385c940eddef965470dfb3d4a85951fa3a5d  ./seed10/node8.log
acaebcd6414133a8040c356f4e89d0dc0a2968b99175b032ddd56afe953cc563  ./seed10/node9.log
dadbcef3ff8b509de6933083f649d3047e0d10c3662d75e64d0b0a004b8d09b2  ./seed10/node10.log
17fc586996547060756b15f280c6ad4db805d88cc077962a4b1c0d3772144784  ./seed10/node11.log
f93a5622ed44963d20e731ab4b918eff23d7dc3fcd75dd50a6d4397a03b15bae  ./seed10/node12.log
f88bdd6eed3a530de1b2c653b5d3a53851ed8399d115d3e1c073760d38d87443  ./seed10/node13.log
a690f4424794ff02eacc78dcde18e9b1f570dc603118fe2a5851c794964ac6f7  ./seed10/node14.log
```

## dag_simple_b1b9cc5 (before stack 8c5677e)

| | |
| --- | --- |
| What | FAN 1.0 `dag_simple` (15 nodes, no LFN), seeds 1–10, 300 virtual seconds each, virtual time, no clock drift |
| Stack | `nanostack-generic-standalone` b1b9cc5 (EM3 step 5, clock drift) |
| Emulator | `mesh-emulator-orchestrator` 221177b |
| Made with | `sim_utils/vtime/campaign.sh build-vt <workdir> <out> 300 1 2 3 4 5 6 7 8 9 10`, on the `tunvt` devices (`run_info.txt` of each seed) |
| Size | 280 MB, 785 files (`seed1`–`seed10`, `out/` the campaign's outputs, `README.txt` the original note) |
| Compare | `sim_utils/migration/nonreg_dag_simple.sh build-vt $(pwd) /home/yann/mesh-emulator-rework/reference-runs/dag_simple_b1b9cc5`: the same 10 seeds × 300 s with the current build and the legacy medium (`--medium=legacy`: the central medium, the default in virtual time since EM4, postdates these runs), each compared with `sim_utils/vtime/compare_logs.py --mask-pointers --mask-source-lines`; exit status 1 on any difference. With `TUN=none` (the default) the logs are identical to these, made on `tunvt0`: only printed heap addresses differ, which the masking hides |
| Regenerate | check out the two commits above, build `build-vt` (`docs/VIRTUAL_TIME.md` §2), then the campaign above; the logs must match the checksums below |

The FAN 1.0 logs were identical to these at every stack and emulator commit
up to stack 8c5677e (no LFN code runs without an LFN role), and still are
with a build of a later stack with `-DNANOSTACK_MAC_RETRY_SUN_BACKOFF=OFF`.

### Checksums

`SHA256SUMS` in the run's directory lists every file (sha256 of the list
itself: `00dff5146f05b79e0aac80cbd18b5a50eafcfc1ce5894b346897c9848e878714`; `sha256sum -c SHA256SUMS` from that directory). The files
`compare_logs.py` reads:

```
7f1d8a5380a3ec04577e5f86516a0b6feee5604db7f2500c3b1e5e635c645fa7  ./seed1/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed1/exit_codes.txt
c0445e90c746b1edb15fb3886b029a2e6a48f8c02cb8754d6e1da3494bc6eb21  ./seed1/log_wsbr
7b51f6b7240e4dcbcac541a37550c6c46bdb9f0453ae82138fef066b13da013e  ./seed1/node1.log
02fa9f05dd511166a612f11077f630798c4c6da3d0f235bb0e78f879925f0a61  ./seed1/node2.log
1b251960a689f3eb73edab45f85b6b6a55e78895c0144375b2d3b2730ce8434f  ./seed1/node3.log
7df28456f1c4cd38900f52ab686edfa27a91ab1c753f12b097b53dc672bfb008  ./seed1/node4.log
d844414d5c9318ccbdd9ea715e28ccbcbb424b9861a9b3c6dd4accfa47a51a2e  ./seed1/node5.log
7b29cbfcde014c84c3325bad21b067eeb6a2866ba43598adeff79cc2bcf6716a  ./seed1/node6.log
0ca528f7be748a988cdefe3f68131843c070695a2c94139777643f9f1b882a18  ./seed1/node7.log
c1541377b927a270471e45347466bf0137a08b63149fd3854a6d27343860f036  ./seed1/node8.log
2886f1f2efa3c8726811a9fdf9d1959ad644645792320f04fe14ac63d4b8e040  ./seed1/node9.log
6e6dbcddf1280fccfd8452987966e4ed7fd514fac6cdbee26775364450a359c0  ./seed1/node10.log
4abed12e427e434451367d9194ef87b7efa9a59c047e53932b30cff7cf0ced1a  ./seed1/node11.log
01243251ddf5fc6b59624e6409316dba58cefc48e72ffab40124ddfa5f21f3a4  ./seed1/node12.log
25418f263f2823ef01c6302b951d3593faa604987ad439cf74cbb40db392decb  ./seed1/node13.log
14135055f0089812e36c1749fb12636eb8e01fa05b3d03a7572413efe2c01248  ./seed1/node14.log
d1de26fff53a91d89ee77e823f89e7109895dbd64a55e91cc8ecb2798af0f5c3  ./seed2/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed2/exit_codes.txt
113e0431adb158db01fc8aa248bc2d217a9eaa3b875aabe31b942e52d6dfc1cf  ./seed2/log_wsbr
faca5be5ad7381e77e0cc8555d2d0d7d3b744ff3a82e9c7319a4293c701e07df  ./seed2/node1.log
6732c5a57547890bd6fd36b697cfa6b7ad19e523cfe5223579f7f26b9f63a348  ./seed2/node2.log
1421a36feed7e28813ed8a88bee1526b8ddc05c6a3e5212fb6f1defd42372a6e  ./seed2/node3.log
a2647d6e4e7edcef92b0a779421f2000ce54c12c7bd66399f209752c51b1de18  ./seed2/node4.log
f5c012fc63dceb462a2a87a14b444e58a838d0cc678f8f8f842b2ac1db6ed2cf  ./seed2/node5.log
2604e8301dd6fffeb7b55fc3cc23ce3b34414f6e7199717fa8938933da25c040  ./seed2/node6.log
d7dcb519da1a6fdf1f55edee04a6e45281eb6917ccaf0a79ee341769e3c68939  ./seed2/node7.log
63c3adb15ac60aaf69d071ca7b0c2293876557f1ddf18fd5406dac80ceb66f5c  ./seed2/node8.log
c5eb803bbf664d602731cfd04e7f94746637dd3fc98c4df1ba18cb57815d504e  ./seed2/node9.log
4a9cd209fc5fa2d25e1c4eab2c7d8556fd49a4012150c5527840c7c27f476597  ./seed2/node10.log
a04295634476475855c22af41e2604918fa79b6e1d51840f2d5f9cb9cbf84b0b  ./seed2/node11.log
11497595c7f56ea1f6de4aab4fa6734f11849881992a41316c0ca879fd11283e  ./seed2/node12.log
27f73acc764faf57d92aa76825b510eaf01d920c73ce963e862b460810366a2d  ./seed2/node13.log
e895d79e192c859e62dd58a196121cf8f1ef30f2db45182f85a6401664c8e99a  ./seed2/node14.log
6237053eb63db465454fd835c17fbf0064617417b73c972e27acfa301648b431  ./seed3/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed3/exit_codes.txt
a012823fa85a425d8f14f40d49622684cee8e47d809876ae67f217718fd439a7  ./seed3/log_wsbr
dd99548599916bb1a414776d03bbb52b1015358d288427d1756cfa2cd60e3909  ./seed3/node1.log
cb6c95ba0aef7dc087dbff53aaf27f9a6f15dbdf7d511040331f6388a7ba2097  ./seed3/node2.log
8d30835b44bf97f645fb74685e2117a420865ec26a1ce3fc24f20f1c075cf722  ./seed3/node3.log
9d2c2ac16e4d0ea3a921985b7365fd047846f58856dba66e3178b8617deb4384  ./seed3/node4.log
a98638ef4606a9d864cd7a55041ac02e3e8b71add1c14634082731ac673b8e60  ./seed3/node5.log
ffc3207503c9bc9124ec9ef85eb6d43c5d24ea68409cba3c9899e60ba048efe8  ./seed3/node6.log
839047372901430ee1be6c626a24407b9a505ab007ac09d35696dee4253be960  ./seed3/node7.log
efd1a1e55cbc024bbd707147dfddce38de8125ecd33ae08479f626f7e68e4ab3  ./seed3/node8.log
1b817b51501841aa6521cbff9671b53c3e50156ddec29bf3bdd2c42b3054817b  ./seed3/node9.log
5fe8f398980ded60d26c7ae34c25678fdad637d0ac40e44cf91147e6d370cf6f  ./seed3/node10.log
1c8929e3f72c8ff822d5bcc251cb4b944c29ff3e4bb285f84a01f5cce67aee28  ./seed3/node11.log
a2b06f3009b8eae2727ae3c5211e9bf968919de57ae153276603a100087acfdd  ./seed3/node12.log
0dbbb90733b3acd4b95b9813fe0f3535e15fa9f3a8aab11f1122b18d11c693b6  ./seed3/node13.log
0562239cd1d6694af03b3eb650b403b9253f8613793c76ebd66bfe71c49cb8b8  ./seed3/node14.log
87309c06f281084b05badb05eb9b22ce98128cabc1c959ff38ec57b7157e7fa9  ./seed4/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed4/exit_codes.txt
cac517dcf87acea02d6ce0880f98b0127834e1205cdfed09775a4168b29435f9  ./seed4/log_wsbr
0cf8bbc372b7133b7f6f860df619cda803dc0e7637b48f7dc6fe938dde60df52  ./seed4/node1.log
05f9a19da758f3c105601bf0d0507d90722fb80596196937bc7f08428c79e7ab  ./seed4/node2.log
c2088da69a45e24210616f6c7eb59a7a941d1a06eeb4a5bc0c7ea6529d818ada  ./seed4/node3.log
86ab80a07f8ac918364fb26479a97d9befca1d79e4aaf2e53b3a3ce3a34f8765  ./seed4/node4.log
6ddca33743c83729de4163c6bfe69f4da84f27f91ec82a20badc27614717b9a9  ./seed4/node5.log
60ac078a40233d5d701ad9325af660d775c68cb2caf34aa2e2cdccc215afcdce  ./seed4/node6.log
716ccdb67ce738754e19523d74f5a73b06c84384d3814f37671fd53eea111621  ./seed4/node7.log
f31652b547ddf94cc3d6d1e4d387408f8df1817f998c94e81fe7c8d831439724  ./seed4/node8.log
972f960ca65a05e6efcdd292408858ae0e7ae0a1491e4b4d598e1793f03defb8  ./seed4/node9.log
c61c8931cc7e7129e8926da09d69a4c3328df62ce1925569676a07723a56c3a3  ./seed4/node10.log
9b25531b6c20498cafd86fb379faddcee482321956823816cb1d42868b1c4cb4  ./seed4/node11.log
38e142981483b54a7364384dfaa19490b4d0682eef404fea4fbae406c7f42f25  ./seed4/node12.log
6a1caa29493905a3b0c0af399d78dd0230fbda6819cb2e6dfc4a87831eb4240f  ./seed4/node13.log
38ffea3c267602747ec4b084304fe907c7b16355282ed2b44bd4e9b11cfa9a32  ./seed4/node14.log
1c3330e674258f9576210ae78103ba33e99ec2027c86ae14d2a32edf72f6137d  ./seed5/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed5/exit_codes.txt
862c7c18c81e33a16b150d936edca9b444220db08b81375d1ddef77804b8ad79  ./seed5/log_wsbr
ef69f3d5665180fb2b8b4b58d602b56a94883a0795b03b457ae5688dd67be7da  ./seed5/node1.log
8479fa9073e764aa4979c583fa4aacaf7a08d8e9225e1a880e05639eebeae2d0  ./seed5/node2.log
92ae769577f9dc40bf67f1e9d425f67f18c8ed77ac64fefc51e9458210258b55  ./seed5/node3.log
c15f8c636db6d3d4cf5a7309f7447bf2d9b75a2a98225e764e0fdc6ae2801be4  ./seed5/node4.log
31ab21164a5f689965dba923ab5758ff4112720f21acc50ca6049ca70f096304  ./seed5/node5.log
8b21f9bc08e603ad8a3e67385a40aa3de5e4e1adaf816f56aa765f10d8301f9e  ./seed5/node6.log
c67b39c53948d9c8e37aa80ecb17d395bd69aa06c9630eaeff2d0790c2b802d9  ./seed5/node7.log
7145d9c78b6f4bc805de0bab22b1175e268285923cead90cdd1638e7b6d9fb95  ./seed5/node8.log
dd0d99b84855d2093c0732c1f677de721a7bea9678515d9ed0ae7c36a9840640  ./seed5/node9.log
45c6133f961b8636972011437ba6ea1acec7b807dbd3b9dc3057e09a19066f1c  ./seed5/node10.log
c6e1988070fdc71a358561eb194087299a386464095cfc9aedb4d6ee281ddde4  ./seed5/node11.log
6f52e9ad6e72dc237b7f410ff69163edc21dfa4609810555ee7546bf6796f6f8  ./seed5/node12.log
98a4799850684f774094b12298fa1a71a301d76f064241011f35a5c10d23cdf6  ./seed5/node13.log
a635c3b352578ca09ddfe12a25a570ba13a4b62a4e472b45ce7c6f6908ef8856  ./seed5/node14.log
4af68c170f98ae65c70ebd2c36c96c8032a1f76cc17406ec1cfb265210cd4b20  ./seed6/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed6/exit_codes.txt
06de8c1b822a56c777bef2ad66f331fb6320923cf58aa4679f8f62f38529ffab  ./seed6/log_wsbr
6fa3b91cfd5a62dbf6b067b5d8df5ed0d35061c0c95cee1f13130ce3b499e506  ./seed6/node1.log
682a2a365a8f6fa5e7938b27bfb9e9ac5bf8d240e481d62abc6f1375229638ea  ./seed6/node2.log
1289b1732a0207b4d1c02fe3a511fd04b3b2aab862adfe34d3757c5e77a6a9c1  ./seed6/node3.log
5b6a763ce645d691759acd36d0deb6bba23edbe67e6d367be86218f46ab01bc8  ./seed6/node4.log
9ecdaed08a718080d421eee4e030347b4b3c20b7d446ebf6fef711628d21771e  ./seed6/node5.log
379d20b55c6b7ab2b8f8372d74a8e3a29c673f055578d3cc950d3a89d19021ae  ./seed6/node6.log
580035e6ea656342173d72e643e5778c23a3592c2cd7f10b8251fed7f72e376f  ./seed6/node7.log
4aa67799358f7983f47aff7187b07ec3a767f47ae5e00c5b1793c3d9e4048b65  ./seed6/node8.log
1e0949e04da5608bcc95d3b5900a6fde25a9226bb5b2a03cddd73793c4c30a41  ./seed6/node9.log
8e8e135506469e74bcc18c739c29a84f74697f869ca3ef3bc581cf1665aa7cc9  ./seed6/node10.log
5231f8917432d796e0fc33b4b6f96bceb7bd989b099b15309fc9bb79c7dca9dd  ./seed6/node11.log
ede014401b74cc65b02b81d97fd3d91952bf01c0413dd882bd1265853f358dc7  ./seed6/node12.log
bf7edecdfa8087f32c951cd7b262f7abc74979550a247e9289241bbffcdcbfc4  ./seed6/node13.log
a0d8188a24ef53206a2f003c0486469ef9fa0bffc58e0fe1900e897dbd506b27  ./seed6/node14.log
12ed97ffe4534f8519e86c49822e4b98430d38014fe2c6bc0d483931720b25c5  ./seed7/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed7/exit_codes.txt
b8af766681617f0a0838d4a727b2eac040b1b149fff098c38382b7a025c00fcb  ./seed7/log_wsbr
feef8065203303100f1eecf872b763d42876ea5a4f832436ce9c2c21ab92cb46  ./seed7/node1.log
36b321393d5ef1e8e60b188ea705ff411750767f672f577676774b4a537df55a  ./seed7/node2.log
be6f61d9a883c595811db481e08e2f2c729f151bba3ca622deb82f54c03c1898  ./seed7/node3.log
ced1e70ac67ab80b4001b66e56830902c3237ca982947a38e94392f96e9f55fb  ./seed7/node4.log
cf9d52700e8c1df6c7f13bc889dea528c11b1e936f8b5264cb05725ae292e46f  ./seed7/node5.log
ea8b61f891d60bb36fd94b42e336dc717448a56967c44f305a83009e294478d3  ./seed7/node6.log
461c04483b30276bc93311d598e69226c6f7761f5c44bbfc42776b0bae79f22b  ./seed7/node7.log
a1ba5d764c1d7af26237c24074e8952b08f9c4ab27e8712c21f245810440b1b4  ./seed7/node8.log
cfa575ab709a20a0d5218f6947edf9a75944c7ce9e4bfba725fd38d15716cc3b  ./seed7/node9.log
9205f25d32215716b267e90d2d9d6b2cc2da938d8cf510af389f9eabb0531d8b  ./seed7/node10.log
d0a3b68d69c7210208441736fda67b49c29ff6e0d47875899489578ce59a4803  ./seed7/node11.log
bb85270af03d9c9ddd8a6bc9005cf836f9da52d4d3a822e3a04c9d2d211f8df6  ./seed7/node12.log
9784088446a512c9dfc079bbcd291ab1c8d7848d9a62b133e3f132c87888a50f  ./seed7/node13.log
ec945135e9c2f9e7388693a01d5a23f7a9ed873a56b754df13a6c791df92bad0  ./seed7/node14.log
aa61726a0c4b2bcc70255e19b323a591fcf0a7a53359fc7f75b053ffff38b4e6  ./seed8/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed8/exit_codes.txt
a51ec84dddb7bc5fc6f2650e4dda0d99a89c72caef3d914c19f5d80c552e3d07  ./seed8/log_wsbr
9dd040961fd4425d09f7eaa91d6ec15762569ad630e6c5a2b9a6157d13d369a7  ./seed8/node1.log
151ca21ad8845342ee9fa7f69a268fe366b574eea396ff8149ab73bdeaf6b8ae  ./seed8/node2.log
d19a20116f04a07272b9a6fb19babd03c249988fa5205b4734f652fd53bd6392  ./seed8/node3.log
61cd6665d71f3985aefdd2c3201c8d1672f842abcf57383472b37baf38c26763  ./seed8/node4.log
a72f1aad03d0c28f1689405d9bc95c80048eef6984a3a6ed337fd842b75fec1e  ./seed8/node5.log
a087964c12d03287c66f18903b54c038220b5e5628ffd5ec646f4ed80587f08d  ./seed8/node6.log
b2097c20cd74fb75c5190fba1a835605bd079c449ebb958f23f902245cfaf311  ./seed8/node7.log
35d858b7dce9c736894c7b1ebf1b3aff16321871ef60ece9d035c1e9f02a4f6c  ./seed8/node8.log
e77b29cef31396a5e074388bce43af0f24d257d7f6f32f16ed5ab0a861530006  ./seed8/node9.log
653f707a211aaf4055f4ad1954d4d982497c07e43af4c4234ddc260bf881be88  ./seed8/node10.log
a3dc3f060c199f23b650e12adadc1d1fec1efa44ad76c823fc1a51412a04f9e5  ./seed8/node11.log
8405d68a2c053beef7b52635610bdb88eeb4e3e3743777f996c78707b929702c  ./seed8/node12.log
82746924fbd53ee15af604a57358046fe4764f424049ce347022607ff8fc6865  ./seed8/node13.log
c2502f4b406528858939c3ff1d998e0d14946c589715b2f728fa2e3e4223de05  ./seed8/node14.log
deac62e69fc628fa17034a72e78d089288500b2c617b0fffc62267a1ff398566  ./seed9/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed9/exit_codes.txt
2f607e3aecfac6ba760e66a6c7a374a33ae48ed8b42db2c531be0f92d4ae1182  ./seed9/log_wsbr
8c78e0d5305dd879e4fe375ce3981f2bf90ead1fb1d855d2160c9549f7cc3140  ./seed9/node1.log
f8c32b4dfacb9e89d508d90364ef1c28ce41ecdb68d87901e0d0b6ac84411c22  ./seed9/node2.log
0c99a2833fe5fb125bdfedcbc5b28c87423724ff285afb7e0cf6080950d4a0e8  ./seed9/node3.log
a935b0cdf6fcdf7da8d9df395fe8979b1356b88a2a8b7fc14aa18873073da3c1  ./seed9/node4.log
d6550f74e4633a5df802b97abadecaa172f57e837f63c920c72449ea89527ce9  ./seed9/node5.log
8380b222e873586e39fdc938a1c3ea101e0b249b614be4640ff53f009a515c47  ./seed9/node6.log
c29c36b19b088c15ef1fd91df324b0a737cda8bc0c2e692b976a0e67e94c1ce2  ./seed9/node7.log
3a205440b086250cb77d2aee78e690cf08441ef564b629add647a9371e28cead  ./seed9/node8.log
9137edcd216665b0bc6c5852eb011c29d88d14193cdb6d76613057f94161ac33  ./seed9/node9.log
faf4638d09146198fbe525df4ffbcc691c2c08140ca7993c19abc235bd39cd71  ./seed9/node10.log
6a4162e505965a48a0fbfc06afb8a83ae531a2f629f6b3d0c4a71817f42bdec3  ./seed9/node11.log
ad3556fcbe4f66e294451e56940f7304a8b028b9d163f4fb5e340a87d76aeeb0  ./seed9/node12.log
059c7d29e634780ceea5bbbdb5672d795bdca8037af27d53675274ec161fb1df  ./seed9/node13.log
26e2d44bc2c59963ee3dfb45a61932c8032ff6db3b533980d53d00f7ace1cde5  ./seed9/node14.log
2077ba148e2c2dadb4c03a36be7980206fb61687703a9254104ec7813f9e4fc9  ./seed10/br.log
9deed1c6f25cfbed5efd38104aa808016fe39c73b1ff264dc6378eddf7eb3de4  ./seed10/exit_codes.txt
50d82344f1893fe9fd8ecccf94b002bfd1a2819eb941c2e346da384cb2496cdf  ./seed10/log_wsbr
1c7b4fe8036fc1a4ede100444bc77769c00e320af94aad04fbe28962749cb1ec  ./seed10/node1.log
34a4e82b54410266ad2d7bc358028e16eb9506e26a7b5e07fa2778d299541b9a  ./seed10/node2.log
5004175a8efbef1f106262fd0c1666f97530d3656cb773877a364ca1803eca56  ./seed10/node3.log
fdac3e3a489ea4bce2a835425030f23e99806a6665d9da0f00e76586f70b9e9b  ./seed10/node4.log
2a32587e1cd09e1caa483a42a3ba4b0443b2b458c760add21e4d6729e27895d8  ./seed10/node5.log
28499027a4d994c50dee01766f90783b6e39ec273bbacf21852c006888e326e4  ./seed10/node6.log
9e83446543240aea292102e79632a6451d9ebd16e868e0eedf0953c62e3ad191  ./seed10/node7.log
06f22ef2a22f0f2c4049499a42330139249721c08f9d7d4de96f12a428fe4fa1  ./seed10/node8.log
0b5f1546b6df1d14fe97b8111b7caf506dc39a2ce7b796fa097921a50398c94b  ./seed10/node9.log
8e2a56e754de58a5c8c345a87290b69ef2e330d13b3eb88148ec451b482a9518  ./seed10/node10.log
a156a55b258a3aa69be5b6760f142871558c195811cfaaac2b4ef00649195aa2  ./seed10/node11.log
46030658ab49a3f1ebba741fb4e6e5237105be55c3944df0471c2c4ede238568  ./seed10/node12.log
dd346ee51c8587b34f66717e1420442dd042a8dd5a8edddf3feb2c280564715f  ./seed10/node13.log
42261a26507757f555a786d9179bce1ca571495a8ad07143a7950bec93e7bdf4  ./seed10/node14.log
```

## MTS bench measurements of 2026-11-19

Measurements on the MTS bench, kept as the baseline of the bench–emulator comparison (TB5). Not byte-for-byte
references: hardware runs do not repeat exactly. Wi-SUN 0.2.9 on all 13 boards, `ws.min_sens` -93 dBm, fixed
attenuator 40 dB. Results and method: `mesh-testbed-controller/docs/BENCH_MEASUREMENTS.md` §1–§4 and
`documents/pre-study/investigation/Test_Bench_MTS.md` §1.6; scripts in `mts-tools`.

| Folder | What | Bench |
| --- | --- | --- |
| `bench_equiv_2026-11-19` | Run 1 of the first equivalence series (BR -13 dBm, attenuators 70 / 20 dB): formation and RTT per hop at 13:13–13:20; its outage part ran after 13:20, when attenuator 1 was put back in the path. Not used in the comparison | Attenuator 1 **bypassed** until about 13:20 |
| `bench_att1_2026-11-19` (+ `.log`) | Attenuator 1 sweep, cold start per setting: 0, 20, 40 dB with attenuator 2 at 30 dB (`bench_rsl_sweep.py` format) | Corrected |
| `bench_att2_2026-11-19` (+ `.log`) | Attenuator 2 sweep: 0, 20, 40 dB with attenuator 1 at 24 dB | Corrected |
| `bench_equiv_2026-11-19_fixed` | Five equivalence runs at 24 / 24 dB, every node +14 dBm (`bench_equiv.py`: `runs.jsonl`, a snapshot per run, `log.txt`); `emulator/`: the scenario, each run's measured topology, the emulator's `report.json` of seeds 1–6 per run, `comparison.md`. Run 1's `detected_s` is invalid (script bug, kept as `detected_s_invalid`) | Corrected |
| `bench_form_24-0_2026-11-19` | Four formations at 24 / 0 dB (`bench_3hop.py`), to test whether the join gap depends on the layer 2–3 link level (it does not) | Corrected |

## MTS bench measurements of 2026-11-20: searching routers and the discovery-channel bug

Same bench state as above (corrected cabling, Wi-SUN 0.2.9, every node +14 dBm, attenuators 24 / 24 dB during the
cycles). Results: `mesh-testbed-controller/docs/BENCH_MEASUREMENTS.md` §4; scripts in `mts-tools`
(`bench_search_channels.py`, `search_channel_analyse.py`, `search_radio_counters.py`, `search_compare.py`).

| Folder | What |
| --- | --- |
| `bench_search_2026-11-20` | 6 cycles of the four layer-3 nodes reset together, original 0.2.9: their channel and parent every second, consoles |
| `bench_search33_2026-11-20` | 4 such cycles with the radio counters every second, while searching and for 60 s after joining |
| `bench_searchfix_2026-11-20` | 6 cycles with a first, partial fix on layer 3 (0.2.9 build 2: tune to the fixed channel in the FHSS); superseded |
| `bench_searchsingle_2026-11-20` | Start of a series resetting one layer-3 node at a time; stopped after the formation (no cycle) |
| `bench_searchdisc_2026-11-20` | 6 cycles with the discovery fix on layer 3 (stack `fix-discovery-channel` `d776ecb`, 0.2.9 build 3) |
| `bench_coldfix_2026-11-20` | 5 cold starts with the discovery fix on all 12 routers (`bench_3hop.py`), consoles; `restore_029.txt` the return to the original 0.2.9; `independent_investigation/` the parallel investigation's patches, scripts and emulator results |
