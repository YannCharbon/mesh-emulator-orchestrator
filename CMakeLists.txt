cmake_minimum_required(VERSION  3.16.3)
project(wsbrd)
set(COMPILE_SIMULATION_TOOLS ON CACHE BOOL "Keep unset if you don't consider to develop new features")
list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")
include(GNUInstallDirs)

include(CheckCCompilerFlag)
check_c_compiler_flag(-Wall                  CC_HAVE_WALL)
check_c_compiler_flag(-Wno-stringop-overread CC_HAVE_WNO_STRINGOP_OVERREAD)
check_c_compiler_flag(-Wno-stringop-overflow CC_HAVE_WNO_STRINGOP_OVERFLOW)
check_c_compiler_flag(-funwind-tables        CC_HAVE_UNWIND_TABLES)
check_c_compiler_flag(-rdynamic              CC_HAVE_RDYNAMIC)

include(CheckIncludeFile)
check_include_file(backtrace.h BACKTRACE_FOUND)

set(THREADS_PREFER_PTHREAD_FLAG ON)
find_package(Threads REQUIRED)

# Syntax "MbedTLS 2.18...<4" does not work :(
find_package(MbedTLS REQUIRED)
mark_as_advanced(MbedTLS_DIR)

find_package(PkgConfig REQUIRED)
# Note that old libpcap releases didn't provide pkg-config file and are not
# detected by the line below. The user have to compile without support for
# libpcap or manually defines the flags.
pkg_check_modules(LIBPCAP              IMPORTED_TARGET libpcap)
pkg_check_modules(LIBSYSTEMD           IMPORTED_TARGET libsystemd)
pkg_check_modules(LIBNL_ROUTE REQUIRED IMPORTED_TARGET libnl-route-3.0)

include(CheckSymbolExists)
set(CMAKE_REQUIRED_LIBRARIES ${CMAKE_DL_LIBS})
set(CMAKE_REQUIRED_DEFINITIONS -D_GNU_SOURCE)
check_symbol_exists(dladdr dlfcn.h LIBDL_FOUND)

# Greatly improve warning messages. There is no reason to not enable that.
if(CC_HAVE_WALL)
    add_compile_options(-Wall)
endif()

# ... but it generate two irrelevant warnings
if(CC_HAVE_WNO_STRINGOP_OVERREAD)
    add_compile_options(-Wno-stringop-overread)
endif()
if(CC_HAVE_WNO_STRINGOP_OVERFLOW)
    add_compile_options(-Wno-stringop-overflow)
endif()

# In come case, backtraces are not available without that flags. Increase size
# of the  binary file, but can be stripped.
if(CC_HAVE_UNWIND_TABLES)
    add_compile_options(-funwind-tables)
endif()

# -rdynamic improves backtraces when debugs symbols are not available. Slightly
# increase size of the binary file (can be stripped?).
if(CC_HAVE_RDYNAMIC AND LIBDL_FOUND)
    add_link_options(-rdynamic)
endif()

# Nanostack is built from the separate nanostack-generic-standalone repository.
# The build type was set further down in wisun-mbed-simulator; it has to be set
# before add_subdirectory() so the stack is compiled with the same flags.
set(CMAKE_BUILD_TYPE Debug)
set(NANOSTACK_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../nanostack-generic-standalone
    CACHE PATH "Path to the nanostack-generic-standalone repository")
# All TLS randomness comes from the port (ns_port_random_bytes(): /dev/urandom
# on the POSIX port, the conductor's seed under virtual time). mbedTLS must be
# built without its own platform entropy (README, "Compile mbedtls"), so the
# port's source has to be registered as strong. A normal variable, so that it
# also applies to build directories configured before (the stack's option()
# honours it, policy CMP0077).
set(NANOSTACK_TLS_STRONG_PORT_ENTROPY ON)
add_subdirectory(${NANOSTACK_DIR} nanostack-build)
nanostack_add_library(nanostack_ws_border_router CONFIG ws_border_router BORDER_ROUTER
                      DEFINITIONS EXTRA_DEBUG_INFO)
if(COMPILE_SIMULATION_TOOLS)
    nanostack_add_library(nanostack_ws_router CONFIG ws_router)
    # nanostack_add_rcp_library(nanostack_rcp DEFINITIONS FHSS_CHANNEL_DEBUG)
    # nanostack_add_rcp_library(nanostack_rcp DEFINITIONS TIMING_TOOL_TRACES)
    # nanostack_add_rcp_library(nanostack_rcp DEFINITIONS EXTRA_DEBUG_INFO)
    nanostack_add_rcp_library(nanostack_rcp)
    # The same MAC for nodes that run it in-process (wsnode-inproc, wsbrd-inproc)
    nanostack_add_rcp_library(nanostack_rcp_inproc INPROCESS)
endif()

add_custom_target(check_git
    ALL
    BYPRODUCTS version.c
    COMMENT "Checking the git repository for changes..."
    COMMAND ${CMAKE_SOURCE_DIR}/version.sh ${CMAKE_SOURCE_DIR} version.c)

add_executable(wsbrd
    version.c
    app_wsbrd/wsbr.c
    app_wsbrd/node_table.c
    app_wsbrd/multicast_helper.c
    app_wsbrd/wsbr_mac.c
    app_wsbrd/wsbr_fhss_mac.c
    app_wsbrd/wsbr_fhss_net.c
    app_wsbrd/tun.c
    app_wsbrd/named_values.c
    app_wsbrd/commandline.c
    app_wsbrd/os_whiteboard.c
    app_wsbrd/mbedtls_config_check.c
    application_protocols/mqtt/MQTTClient-C/src/MQTTClient.c
    #application_protocols/mqtt/MQTTClient-C/src/linux/MQTTLinux.c
    application_protocols/mqtt/MQTTClient-C/src/mbed/MQTTMbed.c
    application_protocols/mqtt/MQTTPacket/src/MQTTPacket.c
    application_protocols/mqtt/MQTTPacket/src/MQTTConnectClient.c
    application_protocols/mqtt/MQTTPacket/src/MQTTConnectServer.c
    application_protocols/mqtt/MQTTPacket/src/MQTTDeserializePublish.c
    application_protocols/mqtt/MQTTPacket/src/MQTTFormat.c
    application_protocols/mqtt/MQTTPacket/src/MQTTSerializePublish.c
    application_protocols/mqtt/MQTTPacket/src/MQTTDeserializePublish.c
    application_protocols/mqtt/MQTTPacket/src/MQTTSubscribeClient.c
    application_protocols/mqtt/MQTTPacket/src/MQTTSubscribeServer.c
    application_protocols/mqtt/MQTTPacket/src/MQTTUnsubscribeClient.c
    application_protocols/mqtt/MQTTPacket/src/MQTTUnsubscribeServer.c
    common/bus_uart.c
    common/spinel_buffer.c
)
target_include_directories(wsbrd PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    application_protocols/mqtt/MQTTClient-C/src/
    application_protocols/mqtt/MQTTClient-C/src/linux/
    application_protocols/mqtt/MQTTClient-C/src/mbed/
    application_protocols/mqtt/MQTTPacket/
)
target_compile_definitions(wsbrd PRIVATE MQTTCLIENT_PLATFORM_HEADER=MQTTMbed.h)
#add_compile_definitions(MQTT_TASK)
target_compile_definitions(wsbrd PRIVATE EXTRA_DEBUG_INFO)
target_link_libraries(wsbrd PRIVATE nanostack_ws_border_router)
target_link_libraries(wsbrd PRIVATE Threads::Threads)
target_link_libraries(wsbrd PRIVATE PkgConfig::LIBNL_ROUTE)
target_link_libraries(wsbrd PRIVATE MbedTLS::mbedtls MbedTLS::mbedcrypto MbedTLS::mbedx509)
if(LIBSYSTEMD_FOUND)
    target_compile_definitions(wsbrd PRIVATE HAVE_LIBSYSTEMD)
    target_sources(wsbrd PRIVATE app_wsbrd/dbus.c)
    target_link_libraries(wsbrd PRIVATE PkgConfig::LIBSYSTEMD)
endif()
if(LIBDL_FOUND)
    target_compile_definitions(wsbrd PRIVATE HAVE_LIBDL)
    target_link_libraries(wsbrd PRIVATE ${CMAKE_DL_LIBS})
endif()
install(TARGETS wsbrd RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
# FIXME: use ${CMAKE_INSTALL_LOCALSTATEDIR}/lib/wsbrd and report this value in
# the code
install(DIRECTORY DESTINATION /var/lib/wsbrd)
install(FILES misc/com.silabs.Wisun.BorderRouter.service
        DESTINATION ${CMAKE_INSTALL_DATAROOTDIR}/dbus-1/system-services)
install(FILES misc/wisun-borderouter.service
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/systemd/system)
# We try to avoid PATH hardcoding, but dbus does not watch
# /usr/local/share/dbus-1/system.d
install(FILES misc/com.silabs.Wisun.BorderRouter.conf
        DESTINATION /etc/dbus-1/system.d)
install(FILES CHANGES.md README.md
        DESTINATION ${CMAKE_INSTALL_DOCDIR})
install(FILES examples/wsbrd.conf examples/br_cert.pem examples/br_key.pem
              examples/ca_cert.pem examples/node_cert.pem examples/node_key.pem
        DESTINATION ${CMAKE_INSTALL_DOCDIR}/examples)

if(COMPILE_SIMULATION_TOOLS)

    add_executable(wsnode
        version.c
        app_wsbrd/multicast_helper.c
        app_wsbrd/wsrouter.c
        app_wsbrd/wsbr_mac.c
        app_wsbrd/wsbr_fhss_mac.c
        app_wsbrd/wsbr_fhss_net.c
        app_wsbrd/commandline.c
        app_wsbrd/named_values.c
        application_protocols/mqtt/MQTTClient-C/src/MQTTClient.c
        #application_protocols/mqtt/MQTTClient-C/src/linux/MQTTLinux.c
        application_protocols/mqtt/MQTTClient-C/src/mbed/MQTTMbed.c
        application_protocols/mqtt/MQTTPacket/src/MQTTPacket.c
        application_protocols/mqtt/MQTTPacket/src/MQTTConnectClient.c
        application_protocols/mqtt/MQTTPacket/src/MQTTConnectServer.c
        application_protocols/mqtt/MQTTPacket/src/MQTTDeserializePublish.c
        application_protocols/mqtt/MQTTPacket/src/MQTTFormat.c
        application_protocols/mqtt/MQTTPacket/src/MQTTSerializePublish.c
        application_protocols/mqtt/MQTTPacket/src/MQTTDeserializePublish.c
        application_protocols/mqtt/MQTTPacket/src/MQTTSubscribeClient.c
        application_protocols/mqtt/MQTTPacket/src/MQTTSubscribeServer.c
        application_protocols/mqtt/MQTTPacket/src/MQTTUnsubscribeClient.c
        application_protocols/mqtt/MQTTPacket/src/MQTTUnsubscribeServer.c
        common/bus_uart.c
        common/spinel_buffer.c
    )
    target_include_directories(wsnode PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}
        application_protocols/mqtt/MQTTClient-C/src/
        application_protocols/mqtt/MQTTClient-C/src/linux/
        application_protocols/mqtt/MQTTClient-C/src/mbed/
        application_protocols/mqtt/MQTTPacket/
    )
    target_compile_definitions(wsnode PRIVATE MQTTCLIENT_PLATFORM_HEADER=MQTTMbed.h)
    # target_compile_definitions(wsnode PRIVATE EXTRA_DEBUG_INFO)
    target_link_libraries(wsnode PRIVATE nanostack_ws_router)
    target_link_libraries(wsnode PRIVATE Threads::Threads)
    target_link_libraries(wsnode PRIVATE MbedTLS::mbedtls MbedTLS::mbedcrypto MbedTLS::mbedx509)
    if(LIBDL_FOUND)
        target_compile_definitions(wsnode PRIVATE HAVE_LIBDL)
        target_link_libraries(wsnode PRIVATE ${CMAKE_DL_LIBS})
    endif()
    install(TARGETS wsnode RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})

    add_executable(wshwsim
        version.c
        common/bus_uart.c
        common/spinel_buffer.c
        app_wshwsim/sl_wsrcp.c
        app_wshwsim/sl_wsrcp_mac.c
        app_wshwsim/ringbufindex.c
        app_wshwsim/sl_rf_driver.c
    )
    target_include_directories(wshwsim PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}
        app_wshwsim/
        app_wssimserver/
    )
    # Debug defines for the MAC/FHSS code are passed to nanostack_add_rcp_library() above
    target_link_libraries(wshwsim PRIVATE nanostack_rcp)
    target_link_libraries(wshwsim PRIVATE m)
    target_link_libraries(wshwsim PRIVATE Threads::Threads)
    target_link_libraries(wshwsim PRIVATE MbedTLS::mbedtls)
    target_link_libraries(wshwsim PRIVATE util pthread)
    if(LIBSYSTEMD_FOUND)
    target_link_libraries(wshwsim PRIVATE PkgConfig::LIBSYSTEMD)
    endif()
    if(LIBPCAP_FOUND)
        target_compile_definitions(wshwsim PRIVATE HAVE_LIBPCAP)
        target_link_libraries(wshwsim PRIVATE PkgConfig::LIBPCAP)
    endif()
    if(LIBDL_FOUND)
        target_compile_definitions(wshwsim PRIVATE HAVE_LIBDL)
        target_link_libraries(wshwsim PRIVATE ${CMAKE_DL_LIBS})
    endif()
    install(TARGETS wshwsim RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})

    # wsnode and wsbrd with the MAC in the same process, on the simulated radio
    # of wshwsim (GENERICITY_PLAN.md S5). -u names the wssimserver socket, and
    # --eui64 the node's address (wshwsim's -m).
    foreach(variant wsnode-inproc wsbrd-inproc)
        if(variant STREQUAL "wsnode-inproc")
            set(app_sources app_wsbrd/wsrouter.c)
            set(nslib nanostack_ws_router)
        else()
            set(app_sources app_wsbrd/wsbr.c app_wsbrd/node_table.c app_wsbrd/tun.c app_wsbrd/os_whiteboard.c
                            app_wsbrd/mbedtls_config_check.c)
            set(nslib nanostack_ws_border_router)
        endif()
        add_executable(${variant}
            version.c
            ${app_sources}
            app_wsbrd/multicast_helper.c
            app_wsbrd/commandline.c
            app_wsbrd/named_values.c
            application_protocols/mqtt/MQTTClient-C/src/MQTTClient.c
            application_protocols/mqtt/MQTTClient-C/src/mbed/MQTTMbed.c
            application_protocols/mqtt/MQTTPacket/src/MQTTPacket.c
            application_protocols/mqtt/MQTTPacket/src/MQTTConnectClient.c
            application_protocols/mqtt/MQTTPacket/src/MQTTConnectServer.c
            application_protocols/mqtt/MQTTPacket/src/MQTTDeserializePublish.c
            application_protocols/mqtt/MQTTPacket/src/MQTTFormat.c
            application_protocols/mqtt/MQTTPacket/src/MQTTSerializePublish.c
            application_protocols/mqtt/MQTTPacket/src/MQTTSubscribeClient.c
            application_protocols/mqtt/MQTTPacket/src/MQTTSubscribeServer.c
            application_protocols/mqtt/MQTTPacket/src/MQTTUnsubscribeClient.c
            application_protocols/mqtt/MQTTPacket/src/MQTTUnsubscribeServer.c
            app_wshwsim/sl_rf_driver.c
            app_wshwsim/sl_rf_inproc.c
            app_wshwsim/ringbufindex.c
        )
        target_include_directories(${variant} PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}
            app_wshwsim/
            app_wssimserver/
            application_protocols/mqtt/MQTTClient-C/src/
            application_protocols/mqtt/MQTTClient-C/src/linux/
            application_protocols/mqtt/MQTTClient-C/src/mbed/
            application_protocols/mqtt/MQTTPacket/
        )
        target_compile_definitions(${variant} PRIVATE MAC_INPROCESS MQTTCLIENT_PLATFORM_HEADER=MQTTMbed.h)
        target_link_libraries(${variant} PRIVATE ${nslib} nanostack_rcp_inproc m util Threads::Threads
                              MbedTLS::mbedtls MbedTLS::mbedcrypto MbedTLS::mbedx509)
        # sl_rf_inproc.c keeps the neighbour list the CCA uses.
        target_link_options(${variant} PRIVATE
            -Wl,--wrap=ns_fhss_ws_update_neighbor,--wrap=ns_fhss_ws_drop_neighbor)
        if(NANOSTACK_PORT STREQUAL "nanostack_port_sim")
            # mbedTLS puts time() in the TLS hello randoms: virtual time instead
            # (sl_rf_inproc.c).
            target_link_options(${variant} PRIVATE -Wl,--wrap=time)
        endif()
        if(LIBSYSTEMD_FOUND)
            target_link_libraries(${variant} PRIVATE PkgConfig::LIBSYSTEMD)
        endif()
        if(LIBDL_FOUND)
            target_compile_definitions(${variant} PRIVATE HAVE_LIBDL)
            target_link_libraries(${variant} PRIVATE ${CMAKE_DL_LIBS})
        endif()
    endforeach()
    target_compile_definitions(wsbrd-inproc PRIVATE EXTRA_DEBUG_INFO)
    if(LIBSYSTEMD_FOUND)
        target_compile_definitions(wsbrd-inproc PRIVATE HAVE_LIBSYSTEMD)
        target_sources(wsbrd-inproc PRIVATE app_wsbrd/dbus.c)
    endif()
    target_link_libraries(wsbrd-inproc PRIVATE PkgConfig::LIBNL_ROUTE)

    # wssimserver only borrows the logger from the stack's Linux port. It is
    # compiled here, without the port's HAVE_BACKTRACE, as it was before.
    add_executable(wssimserver
        ${NANOSTACK_ROOT}/port/posix/common/log.c
        app_wssimserver/sim_power.c
        app_wssimserver/wssimserver.c)
    target_include_directories(wssimserver PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}
        ${NANOSTACK_ROOT}/include/
        ${NANOSTACK_ROOT}/port/posix/
        ${NANOSTACK_ROOT}/port/sim/
    )
    target_link_libraries(wssimserver PRIVATE m)
    install(TARGETS wssimserver RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})

endif()

# Virtual-time build (docs/VIRTUAL_TIME.md): only the in-process programs and
# wssimserver run on the simulation port for now; the split programs follow in
# EM3 step 2.
if(NANOSTACK_PORT STREQUAL "nanostack_port_sim" AND COMPILE_SIMULATION_TOOLS)
    set_target_properties(wsbrd wsnode wshwsim PROPERTIES EXCLUDE_FROM_ALL TRUE)
endif()
