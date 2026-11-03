# Permanent reference runs

Runs kept for byte-for-byte non-regression. They live outside every git
repository, in `reference-runs/` at the root of the work tree
(`/home/yann/mesh-emulator-rework/reference-runs/`, ignored by the root
repository's `.gitignore`); this file is the committed copy of
`reference-runs/README.md`. Everything else is reproducible from its commits,
seed and options: virtual-time runs are deterministic.

## dag_simple_b1b9cc5

| | |
| --- | --- |
| What | FAN 1.0 `dag_simple` (15 nodes, no LFN), seeds 1–10, 300 virtual seconds each, virtual time, no clock drift |
| Stack | `nanostack-generic-standalone` b1b9cc5 (EM3 step 5, clock drift) |
| Emulator | `mesh-emulator-orchestrator` 221177b |
| Made with | `sim_utils/vtime/campaign.sh build-vt <workdir> <out> 300 1 2 3 4 5 6 7 8 9 10`, on the `tunvt` devices (`run_info.txt` of each seed) |
| Size | 280 MB, 785 files (`seed1`–`seed10`, `out/` the campaign's outputs, `README.txt` the original note) |
| Compare | `sim_utils/migration/nonreg_dag_simple.sh build-vt $(pwd) /home/yann/mesh-emulator-rework/reference-runs/dag_simple_b1b9cc5`: the same 10 seeds × 300 s with the current build, each compared with `sim_utils/vtime/compare_logs.py --mask-pointers --mask-source-lines`; exit status 1 on any difference. With `TUN=none` (the default) the logs are identical to these, made on `tunvt0`: only printed heap addresses differ, which the masking hides |
| Regenerate | check out the two commits above, build `build-vt` (`docs/VIRTUAL_TIME.md` §2), then the campaign above; the logs must match the checksums below |

The FAN 1.0 logs have been identical to these at every stack and emulator
commit since (no LFN code runs without an LFN role), which is what the
comparison checks.

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
