# Silicon Labs' LFN profiles as our settings, sourced by test_lfn_profiles.sh
# and test_lfn_mcast.sh: sets LI, LIMAX, MPT, MISMATCH, REG, SYNCONLY, BCI,
# BCSP, BCW, LFN_PROF (the LFN's options) and PARENT_PROF (the parents') for
# PROF = test, balanced or eco.
# Silicon Labs' profiles, Simplicity SDK 2025.12: LFN side from
# sl_wisun_lfn_params_api.h (SL_WISUN_PARAMS_LFN_TEST/BALANCED/ECO), parent
# side from border_router/sl_wisun_br_lfn_params_api.h
# (SL_WISUN_BR_PARAMS_LFN_*). Eco's maximum unicast interval (16 776 s) is
# above our 2^31 us limit (LFN study M11): 2 147 483 ms instead.
# Ours: the broadcast windows widen up to drift_ppm (50) x the sync period
# when that exceeds widening_max_us (100 ms): Eco, 1.8 s each side.
case $PROF in
test)     LI=6000;   LIMAX=60000;   MPT=5;  MISMATCH=120;  REG=2160; SYNCONLY=false; BCI=10000;  BCSP=5;  BCW=0 ;;
balanced) LI=60000;  LIMAX=300000;  MPT=20; MISMATCH=3600; REG=3600; SYNCONLY=false; BCI=60000;  BCSP=15; BCW=0 ;;
eco)      LI=300000; LIMAX=2147483; MPT=60; MISMATCH=3600; REG=5040; SYNCONLY=true;  BCI=600000; BCSP=60; BCW=1800000 ;;
*) echo "lfn_profile.sh: profile test, balanced or eco" >&2; exit 2 ;;
esac
LFN_PROF="-o lfn_listen_interval_ms=$LI -o lfn_listen_interval_min_ms=1000 -o lfn_listen_interval_max_ms=$LIMAX
 -o lfn_eapol_listen_interval_ms=1300 -o lfn_maintain_parent_time=$MPT -o lfn_lgtk_max_mismatch_s=$MISMATCH
 -o lfn_registration_lifetime_min=$REG -o lfn_bc_sync_only=$SYNCONLY -o lfn_bc_widening_max_us=$BCW"
PARENT_PROF="-o lfn_bc_interval_ms=$BCI -o lfn_bc_sync_period=$BCSP -o lfn_registration_lifetime_min=$REG"

