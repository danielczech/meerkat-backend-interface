#!/usr/bin/env python

from optparse import OptionParser
import yaml
import json
import logging
import sys
import redis
import numpy as np
from meerkat_backend_interface import redis_tools
from meerkat_backend_interface.logger import log, set_logger

CHANNEL     = redis_tools.REDIS_CHANNELS.alerts  # Redis channel to listen on
STREAM_TYPE = 'cbf.antenna_channelised_voltage'  # Type of stream to distribute
HPGDOMAIN   = 'bluse'

def json_str_formatter(str_dict):
    """Formatting for json.loads

    Args:
        str_dict (str): str containing dict of spead streams (received on ?configure).

    Returns:
        str_dict (str): str containing dict of spead streams, formatted for use with json.loads
    """
    # Is there a better way of doing this?
    str_dict = str_dict.replace('\'', '"')  # Swap quote types for json format
    str_dict = str_dict.replace('u', '')  # Remove unicode 'u'
    return str_dict

def create_addr_list_filled(addr0, n_groups, n_addrs, streams_per_instance):
    """Creates list of IP multicast subscription address groups.
    Fills the list for each available processing instance 
    sequentially untill all streams have been assigned.
    """
    prefix, suffix0 = addr0.rsplit('.', 1)
    addr_list = []
    if(n_addrs > streams_per_instance*n_groups):
        log.warning('Too many streams: {} will not be processed.'.format(n_addrs - streams_per_instance*n_groups))
        for i in range(0, n_groups):
            addr_list.append(prefix + '.{}+{}'.format(int(suffix0), streams_per_instance - 1))
            suffix0 = int(suffix0) + streams_per_instance
    else:
        n_instances_req = int(np.ceil(n_addrs/float(streams_per_instance)))
        for i in range(1, n_instances_req):
            addr_list.append(prefix + '.{}+{}'.format(int(suffix0), streams_per_instance - 1))
            suffix0 = int(suffix0) + streams_per_instance
        addr_list.append(prefix + '.{}+{}'.format(int(suffix0), n_addrs - 1 - i*streams_per_instance))
    return addr_list

def create_addr_list_distributed(addr0, n_groups, n_addrs):
    """Creates list of IP multicast subscription address groups.
    Attempts to divide the number of streams equally by the number
    of available processing instances.     

    Args:
        addr0 (str): first IP address in the list.
        n_groups (int): number of available hashpipe instances.
        n_per_group (int): number of SPEAD stream addresses per instance.

    Returns:
        addr_list (list): list of IP address groups for subscription.
    """
    prefix, suffix0 = addr0.rsplit('.', 1)
    addr_list = []
    extra_addrs = n_addrs%n_groups
    n_per_group = np.ones(n_groups, dtype=int)*(n_addrs/n_groups)
    n_per_group[:extra_addrs] += 1
    for i in range(0, min(n_addrs, n_groups)):
        addr_list.append(prefix + '.{}'.format(int(suffix0)) + '+' + str(n_per_group[i]-1))
        suffix0 = int(suffix0) + n_per_group[i]
    return addr_list

def read_spead_addresses(spead_addrs, n_groups, streams_per_instance):
    """Parses spead addresses given in the format: spead://<ip>+<count>:<port>
    Assumes this format.

    Args:
        spead_addrs (str): string containing spead IP addresses in the format above.
        n_groups (int): number of stream addresses to be sent to each processing instance.

    Returns:
        addr_list (list): list of spead stream IP address groups.
        port (int): port number.
    """
    addrs = spead_addrs.split('/')[-1]
    addrs, port = addrs.split(':')
    try:
        addr0, n_addrs = addrs.split('+')
        n_addrs = int(n_addrs) + 1
        addr_list = create_addr_list_filled(addr0, n_groups, n_addrs, streams_per_instance)
    except ValueError:
        addr_list = [addrs + '+0']
        n_addrs = 1
    return addr_list, port, n_addrs

def cli():
    usage = "usage: %prog [options]"
    parser = OptionParser(usage=usage)
    parser.add_option('-p', '--port', dest='port', type=long,
                      help='Redis port to connect to', default=6379)
    parser.add_option('-c', '--config', dest='cfg_file', type=str,
                      help='Config filename (yaml)', default = 'config.yml')
    (opts, args) = parser.parse_args()
    # if not opts.port:
    #     print "MissingArgument: Port number"
    #     sys.exit(-1)
    main(port=opts.port, cfg_file=opts.cfg_file)

def configure(cfg_file):
    try:
        with open(cfg_file, 'r') as f:
            try:
                cfg = yaml.safe_load(f)
                return(cfg['hashpipe_instances'], cfg['streams_per_instance'][0])
            except yaml.YAMLError as E:
                log.error(E)
    except IOError:
        log.error('Config file not found')

def pub_gateway_msg(red_server, chan_name, msg_name, msg_val, logger):
    msg = '{}={}'.format(msg_name, msg_val)
    red_server.publish(chan_name, msg)
    logger.info('Published {} to channel {}'.format(msg, chan_name))

def cbf_sensor_name(product_id, redis_server, sensor):
    subarray_nr = product_id[-1] # product ID ends in subarray number
    cbf_prefix = redis_server.get('{}:cbf_prefix'.format(product_id))
    cbf_sensor_prefix = '{}:cbf_{}_{}_'.format(product_id, subarray_nr, cbf_prefix)
    return cbf_sensor_prefix + sensor

def main(port, cfg_file):
    log = set_logger(log_level = logging.DEBUG)
    log.info("Starting Coordinator")
    try:
        hashpipe_instances, streams_per_instance = configure(cfg_file)
        log.info('Configured from {}'.format(cfg_file))
    except:
        log.warning('Configuration not updated; old configuration might be present.')
    red = redis.StrictRedis(port=port)
    ps = red.pubsub(ignore_subscribe_messages=True)
    ps.subscribe(CHANNEL)
    try:
        for message in ps.listen():
            msg_parts = message['data'].split(':')
            if len(msg_parts) != 2:
                log.info("Not processing this message --> {}".format(message))
                continue
            msg_type = msg_parts[0]
            product_id = msg_parts[1]
            # Channel
            global_chan = HPGDOMAIN + ':///set'
            if msg_type == 'conf_complete':
                log.info('New subarray built: {}'.format(product_id))
                all_streams = json.loads(json_str_formatter(red.get("{}:streams".format(product_id))))
                streams = all_streams[STREAM_TYPE]
                addr_list, port, n_addrs = read_spead_addresses(streams.values()[0], len(hashpipe_instances), streams_per_instance)
                n_red_chans = len(addr_list)
                # Number of antennas
                ant_key = '{}:antennas'.format(product_id)
                n_ants = len(red.lrange(ant_key, 0, red.llen(ant_key)))
                pub_gateway_msg(red, global_chan, 'NANTS', n_ants, log)
                # Sync time (UNIX, seconds)
                sensor_key = cbf_sensor_name(product_id, red, 'sync_time')   
                sync_time = int(float(red.get(sensor_key))) # Is there a cleaner way to achieve this casting?
                pub_gateway_msg(red, global_chan, 'SYNCTIME', sync_time, log)
                # Port
                pub_gateway_msg(red, global_chan, 'BINDPORT', port, log)
                # Total number of streams
                pub_gateway_msg(red, global_chan, 'FENSTRM', n_addrs, log)
                # Total number of frequency channels    
                n_freq_chans = red.get('{}:n_channels'.format(product_id))
                pub_gateway_msg(red, global_chan, 'FENCHAN', n_freq_chans, log)
                # Number of channels per substream
                sensor_key = cbf_sensor_name(product_id, red, 'antenna_channelised_voltage_n_chans_per_substream')   
                n_chans_per_substream = red.get(sensor_key)
                pub_gateway_msg(red, global_chan, 'HNCHAN', n_chans_per_substream, log)
                # Number of spectra per heap
                sensor_key = cbf_sensor_name(product_id, red, 'tied_array_channelised_voltage_0x_spectra_per_heap')   
                spectra_per_heap = red.get(sensor_key)
                pub_gateway_msg(red, global_chan, 'HNTIME', spectra_per_heap, log)
                # Number of ADC samples per heap
                sensor_key = cbf_sensor_name(product_id, red, 'antenna_channelised_voltage_n_samples_between_spectra')   
                adc_per_spectra = red.get(sensor_key)
                adc_per_heap = int(adc_per_spectra)*int(spectra_per_heap)
                pub_gateway_msg(red, global_chan, 'HCLOCKS', adc_per_heap, log)
                # Coarse channel bandwidth (from F engines)
                # Note: no sign information!  
                sensor_key = cbf_sensor_name(product_id, red, 'adc_sample_rate')
                adc_sample_rate = red.get(sensor_key)
                coarse_chan_bw = float(adc_sample_rate)/2.0/int(n_freq_chans)
                coarse_chan_bw = '{0:.17g}'.format(coarse_chan_bw)
                pub_gateway_msg(red, global_chan, 'CHAN_BW', coarse_chan_bw, log) 
                for i in range(n_red_chans):
                    local_chan = HPGDOMAIN + '://' + hashpipe_instances[i] + '/set'
                    # Destination IP addresses for instance i
                    pub_gateway_msg(red, local_chan, 'DESTIP', addr_list[i], log)
                    # Number of streams for instance i
                    n_streams_per_instance = int(addr_list[i][-1])+1
                    pub_gateway_msg(red, local_chan, 'NSTRM', n_streams_per_instance, log)
                    # Absolute starting channel for instance i
                    s_chan = i*n_streams_per_instance*int(n_chans_per_substream)
                    pub_gateway_msg(red, local_chan, 'SCHAN', s_chan, log)
            if msg_type == 'deconfigure':
                pub_gateway_msg(red, global_chan, 'DESTIP', '0.0.0.0', log)
                log.info('Subarray deconfigured')
            if msg_type == 'capture-start':
                pub_gateway_msg(red, global_chan, 'NETSTAT', 'RECORD', log)
            if msg_type == 'capture-stop':
                pub_gateway_msg(red, global_chan, 'NETSTAT', 'LISTEN', log)
    except KeyboardInterrupt:
        log.info("Stopping coordinator")
        sys.exit(0)
    except Exception as e:
        log.error(e)
        sys.exit(1)

if __name__ == "__main__":
    cli()
