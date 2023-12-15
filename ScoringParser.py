import requests
import threading
import os.path
import math
from pyquery import PyQuery as pq
from flask import Flask, jsonify
import logging
from obswebsocket import obsws, requests as obsreqs
import sys

class ScoringParser():
    def __init__(self, config):
        self._cfg = config
        self._base_addr = config['base_address']
        
        self._stop_connect_retry_flag = threading.Event()
        self._stop_parsing_flag = threading.Event()
        self.CONNECTION_RETRY_DELAY = 1.0
        self.CONNECTION_TIMEOUT = 5.0
        self.PARSING_PERIOD = config['parsing_period']
        
        self.QUAD_COLORS = ['red', 'green', 'blue', 'yellow']
        
        self.connected_status = False

        self.QUICK_RETRY_MAX_CNT = 4
        
        self._between_matches = False
        self._upcoming_matches = {}
        self._quick_rety_cnt = 0
        
        self._cur_match_phase = 'Seeding'
        self._cur_match_num = 0
        self._cur_match_table = {}
        self._prev_match_phase = ''
        self._prev_match_num = 0
        self._prev_match_table = {}
        
        self._cur_web_time = ''
        self._prev_timer_text = None
        
        self._cur_manual_timer_seconds = 0
        self._last_text_timer = ''
        self._last_test_field = []
        
        # inner helper for opening files safely
        def try_open_file(fname, rel_path):
            if fname is None or fname == '':
                return None
            try:
                return open(os.path.join(rel_path, fname), 'w')
            except FileNotFoundError:
                print(f'Could not open file "{os.path.join(rel_path, fname)}", not found.')
                
        if ('use_obs_websocket' not in config) or (not config['use_obs_websocket']):
            # do file-based changes
            # open up all the files:
            self._timer_f = try_open_file(config['timer_file'], config['rel_file_path'])
            self._mnum_f = try_open_file(config['match_num_file'], config['rel_file_path'])
            self._field_fs = {}
            for idx, field in enumerate(config['fields']):
                self._field_fs[idx+1] = {}
                for color in self.QUAD_COLORS:
                    self._field_fs[idx+1][color] = try_open_file(
                                    field[color+'_file'], config['rel_file_path'])
            # set the right label change functions
            self.set_timer_label = self.set_timer_label_file
            self.set_match_label = self.set_match_label_file
            self.set_quadrant_labels = self.set_quadrant_labels_file
        else:
            # do OBS websocket -based changes
            self._obs_client = obsws(config['obs_websocket_addr'], config['obs_websocket_port'], config['obs_websocket_pw'])
            self._obs_client.connect()
            
            
            if self._obs_config_and_validate_text(config['timer_source']):
                self._timer_src = config['timer_source']
            else:
                print('Errors encountered while validating timer source.')
                print('WARNING: Will continue with NO timer source setting.')
                self._timer_src = None
                
            
            if self._obs_config_and_validate_text(config['match_num_source']):
                self._mnum_src = config['match_num_source']
            else:
                print('Errors encountered while validating match num source.')
                print('WARNING: Will continue with NO match num source setting.')
                self._mnum_src = None
                
            self._field_srcs = {}
            for idx, field in enumerate(config['fields']):
                self._field_srcs[idx+1] = {}
                for color in self.QUAD_COLORS:
                    if self._obs_config_and_validate_text(field[color+'_source']):
                        self._field_srcs[idx+1][color] = field[color+'_source']
                    else:
                        print(f'Errors encountered while validating quadrant [{idx+1},{color}] source.')
                        print(f'WARNING: Will continue with NO quadrant [{idx+1},{color}] source setting.')
                        self._field_srcs[idx+1][color] = None
            # set the right label change functions
            self.set_timer_label = self.set_timer_label_obsws
            self.set_match_label = self.set_match_label_obsws
            self.set_quadrant_labels = self.set_quadrant_labels_obsws
            
        # parse team numbers:
        self.parse_team_numbers()
        #print(f'{self.team_name2num=}')
        #print(f'{self.team_num2name=}')
        
        # set up threads
        self._parsing_thread = None
        self._switchover_thread = None
        self._connect_thread = threading.Thread(
                target=self.make_connection_thread_func)
        self._connect_thread.daemon = True
        # start up the connection thread:
        print('Starting...')
        self._connect_thread.start()
        
        
        # set up webserver, if enabled
        if config['host_timer_webserver']:
            self.init_webserver()


    def _obs_config_and_validate_text(self, src_name):
        if src_name is None or src_name == '':
            print(f'ERROR: no source name.')
            return False
            
        resp = self._obs_client.call(obsreqs.GetInputSettings(inputName=src_name))
        
        if not resp.status:
            print(f'ERROR: No matching source name: "{src_name}"')
            return False
        if resp.getInputKind() != 'text_gdiplus_v2':
            print(f'ERROR: Source type for "{src_name}" is "{resp.getInputKind()}"; expected "text_gdiplus_v2"')
            return False
        
        settings = resp.getInputSettings()
        if settings['read_from_file']:
            print(f'Reconfiguring source "{src_name}" to NOT read from file.')
            if not self._obs_client.call(obsreqs.SetInputSettings(inputName=src_name, inputSettings={'read_from_file': False})).status:
                print('ERROR: Failed setting settings on source.')
                return False
        print(f'Clearing text on source "{src_name}".')
        if not self._obs_client.call(obsreqs.SetInputSettings(inputName=src_name, inputSettings={'text': ''})).status:
            print('ERROR: Failed setting text settings on source.')
            return False
        return True
        
    def make_connection_thread_func(self):
        addr = self._base_addr + "/Marquee/Match"
        
        while not self._stop_connect_retry_flag.wait(self.CONNECTION_RETRY_DELAY):
            try:
                resp = requests.get(addr, timeout=self.CONNECTION_TIMEOUT)
                if resp is None or resp.status_code != 200:
                    print(f'Connection failed with response code {resp.status_code}.')
                    continue
                else:
                    print('Connection successful.')
                    self._stop_connect_retry_flag.set()
                    self._stop_parsing_flag.clear()
                    # Start the parsing update thread
                    self._parsing_thread = threading.Thread(
                            target = self.parsing_update_thread_func)
                    self._parsing_thread.daemon = True
                    self.connected_status = True
                    self._parsing_thread.start()
            except requests.exceptions.Timeout:
                print(f'Connection request timed out.')
                # keep looping
    
    def parsing_update_thread_func(self):
        addr = self._base_addr + "/Marquee/Match"
        
        while not self._stop_parsing_flag.wait(self.PARSING_PERIOD):
            
            try:
                resp = requests.get(addr, timeout=self.CONNECTION_TIMEOUT)
            except requests.exceptions.Timeout:
                print('Request timed out while getting update, retrying.')
                resp = None
                
            if resp is not None and resp.status_code != 200:
                print(f'Request failed with status {resp.status_code} while getting update, retrying.')
                resp = None
                
            if resp is None:
                self._quick_rety_cnt += 1
                if self._quick_rety_cnt >= self.QUICK_RETRY_MAX_CNT:
                    print('Too many retries. Connection lost, starting over.')
                    # retried enough, go back to the slower retry thread
                    self._stop_parsing_flag.set()
                    self._stop_connect_retry_flag.clear()
                    self._connect_thread = threading.Thread(
                            target=self.make_connection_thread_func)
                    self._connect_thread.daemon = True
                    self._connect_thread.start()
                    self.connected_status = False
                continue
                
            # connection was good
            if self._quick_rety_cnt != 0:
                print('Connection restored.')
                self._quick_rety_cnt = 0
                self.connected_status = True
            
            # start parsing:
            try:
                root_parse = pq(resp.content)
                need_to_handle_between_matches = False
            except:
                # assume excpetion caused by document being empty, which
                # means that we're between matches
                need_to_handle_between_matches = True
            
            if not need_to_handle_between_matches:
                # Doc isn't empty, so go ahead and check the timer
                # If timer is 00:00, that's the other possible indicator
                # that we actually *are* between matches
                
                # get timer:
                elem_timer = root_parse('.nameAndTimer > h2')
                if not elem_timer:
                    # no timer found, loop over and try again
                    print('Couldn''t find the timer field')
                    continue
                timer_text = elem_timer[0].text
                if (timer_text == '00:00' or timer_text == '0:00') and (self._cur_web_time == ''):
                    pass # don't change the blank timer text field
                else:
                    self._cur_web_time = elem_timer[0].text
                
                if (timer_text == '00:00') or (timer_text == '0:00'):
                    # match is over, so we do need to handle between-match condition
                    need_to_handle_between_matches = True
                    # For the first time (when we're just now becoming between matches)
                    #  make sure it shows the new '00:00' and doesn't get stuck on '00:01'
                    # After the first time, between_matches will be tru, and the upcoming
                    #  switchover logic will take over setting the timer label at the
                    #  appropriate time.
                    if not self._between_matches:
                        self.set_timer_label(self._cur_web_time)
                        
                else:
                    need_to_handle_between_matches = False
                    # Not between matches, time text label will get set later along
                    #  with match num and quads
            
            
            # If we're between matches, handle that now.
            # This 'if' block 'continue's, so no other parsing happens if
            #  need_to_handle_between_matches == True
            if need_to_handle_between_matches:
                # See if we're just now going to be in between matches:
                if (not self._between_matches):
                    self._between_matches = True
                    # grab the upcoming match table
                    self._upcoming_matches = self.parse_upcoming_matches_table()
                    
                    # if cur_match_num is 0, then switch immediately, since there's not
                    #  really an existing match up at that point
                    if (self._cur_match_num is None or self._cur_match_num == 0):
                        self.upcoming_match_switchover()
                    # else, check for auto_switchover to see if we need to auto switch
                    #  between matches
                    elif self._cfg['auto_switchover']:
                        effective_switchover_time = self._cfg['switchover_time']
                        
                        if self._cfg['manual_timer']:
                            # if using manual timer, then check if there's still time left
                            #  and compensate by adding it to the switchover_time
                            effective_switchover_time = effective_switchover_time + \
                                    self._cur_manual_timer_seconds
                        
                        # if it's 0, switch immediately
                        if effective_switchover_time == 0:
                            self.upcoming_match_switchover()
                        else:
                            self._switchover_thread = threading.Timer(
                                    effective_switchover_time,
                                    self.upcoming_match_switchover_timer_func)
                            self._switchover_thread.daemon = True
                            self._switchover_thread.start()
                # nothing else to do when handling between match condition
                continue
                
            # Not handling between matches, handle normal during-match
            self._between_matches = False
            
            # see if there was a switchover scheduled that we need to remove now
            if self._switchover_thread is not None:
                try:
                    self._switchover_thread.cancel()
                except:
                    # could be a case where thread is already running...
                    # just catch it and let it go, it's fine.
                    pass
            
            # get match phase and num
            elem_match_phase_and_num = root_parse('.nameAndTimer > h3')
            if not elem_match_phase_and_num:
                # Not found, re-loop
                print('Couldn''t find the match phase field')
                continue
            split_str = elem_match_phase_and_num[0].text.split(' ')
            self._cur_match_phase = split_str[0]
            try:
                self._cur_match_num = int(split_str[-1])
            except ValueError:
                print(f'Error parsing match number from "{split_str[-1]}".')
                pass
                #self._cur_match_num = 0
            
            # get the field elements
            self._cur_match_table = {}
            # TODO: separate out team number from team name
            elem_field_elements = root_parse('.fields > .field')
            if not elem_field_elements:
                # Not found, re-loop
                print('Couldn''t find the field elements')
                continue
            
            for field_elem in elem_field_elements.items():
                try:
                    field_num = int(field_elem('table > tr > th')[0].text[6:])
                except:
                    print('Failed parsing field number.')
                    continue
                self._cur_match_table[field_num] = {}
                
                for color in self.QUAD_COLORS:
                    elem_quad = field_elem('table > tr > td.light-'+color)
                    if not elem_quad:
                        print(f'Error parsing {color} quad on field {field_num}.')
                        continue
                    self._cur_match_table[field_num][color] = \
                            elem_quad[0].text.strip() # TODO: unescape html?
                        
            
            # set all of the labels
            self.set_all_labels_to_current()
        # end of while self._stop_parsing_flag.wait
    # end of parsing_update_thread_func
    
    def upcoming_match_switchover_timer_func(self):
        # TODO: TBD if I need to do anything else here.
        self.upcoming_match_switchover()
        
    def upcoming_match_switchover(self):
        if len(self._upcoming_matches) > 0:
            if self._cur_match_num == 0:
                # Could be an accidental reset in the middle of the match schedule.
                # Check the upcoming match table for the lowest number and assume
                # that's the next match number.
                lowest_num = min(self._upcoming_matches.keys())
                self._cur_match_num = lowest_num
                self.parse_match_phase()
                print(f'Unsure about last match number. Assuming next match is '+
                      f'{self._cur_match_phase} {self._cur_match_num} based on '+
                      'upcoming match table.')
            else:
                # Advance the match number
                self._cur_match_num += 1
            
            try:
                cur_match_table = self._upcoming_matches[self._cur_match_num]
                self.set_quadrant_labels(cur_match_table)
                self._cur_web_time = '' # !!!
                self.set_timer_label('')
                self.set_match_label(self._cur_match_phase,self._cur_match_num)
            except KeyError:
                # Means no more upcoming matches, we've reached the end of the
                #  current phase
                blank_table = {}
                for ridx in self._field_fs.keys():
                    blank_table[ridx] = {}
                    for color in self.QUAD_COLORS:
                        blank_table[ridx][color] = ''
                self.set_quadrant_labels(blank_table)
                self._cur_web_time = '' # !!!
                self.set_timer_label('')
                self.set_match_label('','')
    # end of upcoming_match_switchover
    
    def parse_upcoming_matches_table(self):
        addr = self._base_addr + '/Marquee/PitRefresh'
        
        try:
            resp = requests.get(addr, timeout=self.CONNECTION_TIMEOUT)
        except requests.exceptions.Timeout:
            print('Request timed out while getting upcoming matches table.')
            return {}
        
        if resp is None:
            print('Request failed while getting upcoming matches table.')
            return {}
            
        if resp.status_code != 200:
            print(f'Request failed with code {resp.status_code} while getting upcoming matches table.')
            return {}
        
        root_parse = pq(resp.content)
        
        ret_dict = {}
        
        # get rows:
        elem_rows = root_parse('table > tbody > tr')
        if not elem_rows:
            # no rows found
            print('Couldn\'t find the rows for the upcoming matches. That probably means we\'re at the end to the current phase.')
            return {}
        
        for elem_row in elem_rows.items():
            elem_match_field = elem_row("td[style='white-space:nowrap']")
            if not elem_match_field:
                # just skip this row
                continue
            match_split = elem_match_field[0].text.split(' - ')
            try:
                match_num = int(match_split[0])
                field_num = int(match_split[1])
            except (IndexError, ValueError):
                print('Failed parsing out upcoming match table match number & field number. Skipping row and attempting to continue.')
                continue
                
            # create empty dicts if they don't exist yet
            if match_num not in ret_dict:
                ret_dict[match_num] = {}
            if field_num not in ret_dict[match_num]:
                ret_dict[match_num][field_num] = {}
            # get the color td's
            for color in self.QUAD_COLORS:
                elem_quad = elem_row('td.'+color)
                if not elem_quad:
                    # fill in with blank
                    ret_dict[match_num][field_num][color] = ''
                else:
                    # TODO: unescape html?
                    ret_dict[match_num][field_num][color] = elem_quad[0].text.strip()
                    
        #print(f'upcoming match table: {ret_dict}')
        return ret_dict
    # end of parse_upcoming_matches_table
    
    def parse_match_phase(self):
        addr = self._base_addr + '/phase'
        
        try:
            resp = requests.get(addr, timeout=self.CONNECTION_TIMEOUT)
        except requests.exceptions.Timeout:
            print('Request timed out while getting phase schedule.')
            return
        
        if resp is None:
            print('Request failed while getting phase schedule.')
            return
            
        if resp.status_code != 200:
            print(f'Request failed with code {resp.status_code} while getting phase schedule.')
            return
        
        root_parse = pq(resp.content)
        
        elem_phase = root_parse('h2')
        if not elem_phase:
            # no phase found
            print('Couldn\'t find phase header line in the phase schedule.')
            return
            
        if elem_phase[0].text[-6:] == ' Phase':
            self._cur_match_phase = elem_phase[0].text[:-6]
        else:
            print(f'Not sure how to parse the phase from the header text "{elem_phase[0].text}".')
    # end of parse_match_phase
    
    def parse_team_numbers(self):
        addr = self._base_addr + '/lookup'
        
        self.team_num2name = {}
        self.team_name2num = {}
        
        try:
            resp = requests.get(addr, timeout=self.CONNECTION_TIMEOUT)
        except requests.exceptions.Timeout:
            print('Request timed out while getting team number lookup.')
            return
            
        if resp is None:
            print('Request failed while getting team number lookup.')
            return
            
        root_parse = pq(resp.content)
        
        elem_team_select = root_parse('#LookupInfo > .row > select.form-control:first-of-type')
        if not elem_team_select:
            # no team list
            print('Couldn\'t find team list selection in lookup page.')
            return
            
        elem_options = elem_team_select('option[selected] ~ option') # skip the first (selected) option, get the rest
        if not elem_options:
            # no team list
            print('Couldn\'t find team list options in lookup page.')
            return
        
        for elem_option in elem_options:
            try:
                team_num = int(elem_option.values()[-1])
                team_name = elem_option.text.split(' (')[0]
                self.team_num2name[team_num] = team_name
                self.team_name2num[team_name] = team_num
            except ValueError:
                print(f'Failed to parse number: "{elem_option.values()[-1]}". Skipping.')
            except IndexError:
                print(f'Indexing error for option with text "{elem_option.text}"')
    
    
    def set_all_labels_to_current(self):
        if not self._cfg['manual_timer']:
            self.set_timer_label(self._cur_web_time)
        # if using manual timer, then the timer label gets set by the
        #   manual timer's countdown function
        
        # set match number and (optionally) phase
        self.set_match_label(self._cur_match_phase, self._cur_match_num)
        
        # only attempt to set quadrant labels if the match table isn't empty
        if len(self._cur_match_table) > 0:
            self.set_quadrant_labels(self._cur_match_table)
    # end of set_all_labels_to_current
    
    def set_timer_label_file(self, timer_text):
        if self._prev_timer_text == timer_text:
            # nothing to do, the lable hasn't changed.
            return
        if self._timer_f is not None:
            # clear the file, write it, and flush it
            self._timer_f.truncate(0)
            self._timer_f.seek(0)
            self._timer_f.write(timer_text)
            self._timer_f.flush()
        self._prev_timer_text = timer_text
    # end of set_timer_label
    
    def set_match_label_file(self, match_phase, match_num, force_rewrite=False):
        if self._prev_match_num == match_num and self._prev_match_phase == match_phase and not force_rewrite:
            # nothing to do, the label hasn't changed.
            return
        if self._mnum_f is not None:
            if self._cfg['show_match_phase']:
                match_string = f'{match_phase} {match_num}'
            else:
                match_string = str(match_num)
            # clear the file, write it, and flush it
            self._mnum_f.truncate(0)
            self._mnum_f.seek(0)
            self._mnum_f.write(match_string)
            self._mnum_f.flush()
        self._prev_match_num = match_num
        self._prev_match_phase = match_phase
    # end of set_match_label
    
    def set_quadrant_labels_file(self, match_table, force_rewrite=False):
        if self._prev_match_table == match_table and not force_rewrite:
            # nothing to do, the table hasn't changed.
            return
        self._prev_match_table = {}
        for field_num in match_table.keys():
            for color in self.QUAD_COLORS:
                if self._field_fs[field_num][color] is not None:
                    # clear the file, write it, and flush it
                    self._field_fs[field_num][color].truncate(0)
                    self._field_fs[field_num][color].seek(0)
                    self._field_fs[field_num][color].write(
                            match_table[field_num][color])
                    self._field_fs[field_num][color].flush()
            # need to copy one field at a time to prev match table (to get a deep copy)
            self._prev_match_table[field_num] = match_table[field_num].copy()
    # end of set_quadrant_labels
    
    def set_timer_label_obsws(self, timer_text):
        if self._prev_timer_text == timer_text:
            # nothing to do, the lable hasn't changed.
            return
        if self._timer_src is not None:
            if not self._obs_client.call(obsreqs.SetInputSettings(
                        inputName=self._timer_src,
                        inputSettings={'text': timer_text}
                    )).status:
                print('ERROR: Failed to set timer text via OBS websocket.')
                return
        self._prev_timer_text = timer_text
    # end of set_timer_label
    
    def set_match_label_obsws(self, match_phase, match_num, force_rewrite=False):
        if self._prev_match_num == match_num and self._prev_match_phase == match_phase and not force_rewrite:
            # nothing to do, the label hasn't changed.
            return
        if self._mnum_src is not None:
            if self._cfg['show_match_phase']:
                match_string = f'{match_phase} {match_num}'
            else:
                match_string = str(match_num)
            if not self._obs_client.call(obsreqs.SetInputSettings(
                        inputName=self._mnum_src,
                        inputSettings={'text': match_string}
                    )).status:
                print('ERROR: Failed to set match number text via OBS websocket.')
                return
        self._prev_match_num = match_num
        self._prev_match_phase = match_phase
    # end of set_match_label
    
    def set_quadrant_labels_obsws(self, match_table, force_rewrite=False):
        if self._prev_match_table == match_table and not force_rewrite:
            # nothing to do, the table hasn't changed.
            return
        self._prev_match_table = {}
        
        for field_num in match_table.keys():
            had_error = False
            for color in self.QUAD_COLORS:
                if self._field_srcs[field_num][color] is not None:
                    if not self._obs_client.call(obsreqs.SetInputSettings(
                                inputName=self._field_srcs[field_num][color],
                                inputSettings={'text': match_table[field_num][color]}
                            )).status:
                        print(f'ERROR: Failed to set quadrant [{field_num},{color}] text via OBS websocket.')
                        had_error = True
            if not had_error:
                # need to copy one field at a time to prev match table (to get a deep copy)
                self._prev_match_table[field_num] = match_table[field_num].copy()
    # end of set_quadrant_labels
    
    
    def set_manual_timer_text(self):
        seconds = math.floor(self._cur_manual_timer_seconds % 60)
        minutes = math.floor(self._cur_manual_timer_seconds / 60)
        timer_text = f'{minutes:01d}:{seconds:02d}'
        self.set_timer_label(timer_text)
        
    def init_webserver(self):
        try:
            ip = self._cfg['webserver_hostip']
        except KeyError:
            ip = '0.0.0.0'
        try:
            port = self._cfg['webserver_port']
        except KeyError:
            port = 9269
        
        app = Flask(__name__)
        
        @app.route('/timer')
        def timer_page():
            page = '<html><head><style>body {background-color: black;' +\
               'font-family: "Trebuchet MS", sans-serif;' +\
               '}</style></head><body> ' +\
               '<div style="width: 100%; height: auto; bottom: 0px; top: 0px; left: 0; position: absolute;"> ' +\
               '<div id="timer" style="height: 100vh; display: flex; justify-content: center; align-items: center; ' +\
               'font-size: 50vh; color: white;">00:00</div></div></body></html>' +\
               '<script type="text/javascript" src="'+self._cfg['base_address']+'/js/jquery-3.4.1.min.js"></script>' +\
               '<script type="text/javascript" src="'+self._cfg['base_address']+'/js/bootstrap.min.js"></script>' +\
               '<script type="text/javascript" src="'+self._cfg['base_address']+'/js/jquery-ui.min.js"></script>' +\
               '<script type="text/javascript" src="'+self._cfg['base_address']+'/js/jquery.ba-throttle-debounce.min.js"></script>' +\
               '<script>' +\
               'function RefreshMatch() { var jqxhr = $.get("/timer.json", function(data) { $("#timer").html(data.timer); })' +\
               '.fail(function() { $("#timer").html("---");} );}' +\
               '''
                $(document)
                    .ready(function() {
                        $('.navbar').hide();
                        $('body').css('grid-template-rows', 'auto');
                        setInterval(RefreshMatch, 100);
                    });
                ''' +\
               '</script>'
            return page
        @app.route('/timer.json')
        def timer_json():
            return jsonify({'timer': self._cur_web_time})
            
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        threading.Thread(target=lambda: app.run(host=ip, port=port, debug=False, use_reloader=False)).start()