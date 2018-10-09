import math
import itertools
import subprocess
import os
import numpy as np
import sys
import h5py
import shutil
import argparse
from collections import OrderedDict
import re


class saltproc:
    """ Class saltproc runs SERPENT and manipulates its input and output files
        to reprocess its material, while storing the SERPENT run results in a
        HDF5 database. 
    """

    def __init__(self, steps, cores, nodes, bw, exec_path, restart=False,
                 input_file='core', db_file='db_saltproc.hdf5',
                 mat_file='fuel_comp', init_mat_file='init_mat_file',
                 driver_mat_name='fuel', blanket_mat_name='',
                 blanket_vol=0, driver_vol=1, rep_scheme={}):
        """ Initializes the class

        Parameters:
        -----------
        steps: int
            total number of steps for this saltproc run
        cores: int
            number of cores to use for this saltproc run
        nodes: int
            number of nodes to use for this saltproc run
        bw: string
            # !! if 'True', runs saltproc on Blue Waters
        exec_path: string
            path of SERPENT executable
        restart: bool
            if true, starts from an existing database
        input_file: string
            name of input file
        db_file: string
            name of output hdf5 file
        mat_file: string
            name of material file connected to input file
        init_mat_file: string
            name of material file initally definedd by user
        driver_mat_name: string
            name of driver material in the definition
        blanket_mat_name: string
            name of blanket material in definition
        driver_vol: float
            volume of driver
        blanket_vol: float
            volume of blanket
        rep_scheme: dict
            key: scheme name
            value: element, freq, qty, comp, begin_time, end_time, from, to, eff
        """
        # initialize all object attributes
        self.steps = steps
        self.cores = cores
        self.nodes = nodes
        self.bw = bw
        self.exec_path = exec_path
        self.restart = restart
        self.input_file = input_file
        self.db_file = db_file
        self.mat_file = mat_file
        self.current_step = 0
        self.init_mat_file = init_mat_file
        self.blanket_mat_name = blanket_mat_name
        self.driver_mat_name = driver_mat_name
        self.driver_vol = driver_vol
        self.blanket_vol = blanket_vol
        self.get_library_isotopes()
        self.prev_qty = 1

        self.rep_scheme = rep_scheme
        self.set_default_rep_params()
        self.normalize_comp_rep_params()
        self.check_error_rep_params()

        self.two_region = True
        if blanket_mat_name == '':
            self.two_region = False


    def set_default_rep_params(self):
         for group, spec in self.rep_scheme.items():
            if 'freq' not in spec.keys():
                self.rep_scheme[group]['freq'] = -1
            if 'qty' not in spec.keys():
                self.rep_scheme[group]['qty'] = -1
            if 'begin_time' not in spec.keys():
                self.rep_scheme[group]['begin_time'] = -1
            if 'end_time' not in spec.keys():
                self.rep_scheme[group]['end_time'] = 1e299
            if 'from' not in spec.keys():
                self.rep_scheme[group]['from'] = 'fertile'
            if 'to' not in spec.keys():
                self.rep_scheme[group]['to'] = 'waste'
            if 'eff' not in spec.keys():
                self.rep_scheme[group]['eff'] = 1

    def normalize_comp_rep_params(self):
        for group, spec in self.rep_scheme.items():
            if 'comp' in spec.keys():
                self.rep_scheme[group]['comp'] = [
                    x / sum(self.rep_scheme[group]['comp']) for x in self.rep_scheme[group]['comp']]

    def check_error_rep_params(self):
        for group, spec in self.rep_scheme.items():
            if 'element' not in spec.keys():
                raise ValueError('Missing elements for %s' % group)
            if 'from' not in spec.keys() and 'to' not in spec.keys():
                raise ValueError('Missing to AND from for %s' % group)
            if spec['from'] == 'fertile' and 'comp' not in spec.keys():
                raise ValueError('Must define composition for input material')
            if spec['to'] == 'waste':
                for el in spec['element']:
                    if any(char.isdigit() for char in el):
                        raise ValueError('You can only remove Elements')


    def find_iso_indx(self, keyword):
        """ Returns index number of keyword in bumat dictionary

        Parameters:
        -----------
        keyword: string or list
            list - list of elements
            string - isotope

        Returns:
        --------
        numpy array of indices
        """
        indx_list = []
        indx = 0
        if isinstance(keyword, str):
            indx = self.isoname.index(keyword)
            indx_list.append(indx)
        elif isinstance(keyword, list):
            for key in keyword:
                for indx, isotope in enumerate(self.isoname):
                    el = " ".join(re.findall("[a-zA-Z]+", isotope))
                    if key == el:
                        indx_list.append(indx)
        return np.array(indx_list)

    def get_library_isotopes(self):
        """ Returns the isotopes in the cross section library

        Parameters:
        -----------

        Returns:
        --------
        iso_array: array
            array of isotopes in cross section library:
        """
        # check if environment variable is set
        path = self.check_env_variable()
        acelib = self.get_acelib_path()
        self.lib_isos = []
        acelib_path = path + acelib

        with open(acelib, 'r') as f:
            lines = f.readlines()
            for line in lines:
                iso = line.split()[1]
                self.lib_isos.append(iso)

        self.lib_isos = np.array(self.lib_isos)

    def get_acelib_path(self):
        """ Finds and returns the user-defined acelib
            from the SERPENT input file

        Returns:
        --------
        acelib: str
            path to acelib
        """
        with open(self.input_file, 'r') as f:
            lines = f.readlines()
            for line in lines:
                if 'set acelib' in line and '%' != line[0]:
                    start = line.index('"') + 1
                    end = line[start:].index('"') + start
                    acelib = line[start:end]
        return acelib

    def check_env_variable(self):
        """ Checks for environment variable `SERPENT_DATA'

        Returns:
        --------
        path: str
            'SEPRENT_DATA' if the environment variable exists
            '' if it doesn't exist
        """
        if os.environ.get('SERPENT_DATA') is not None:
            path =  os.environ['SERPENT_DATA']
        else:
            path = ''
        return path

    def get_mat_def(self):
        """ Get material definition from the initial material definition
            file
        """
        with open(self.init_mat_file, 'r') as f:
            mat_def_dict = OrderedDict({})
            lines = f.readlines()
            for line in lines:
                if 'mat' in line:
                    z = line.split()
                    key = z[1]
                    line = line.split('%')[0]
                    mat_def_dict[key] = line
        return self.mat_def_dict

    def get_isos(self):
        """ Reads the isotope zai and name from dep file"""
        dep_file = self.input_file + '_dep.m'
        with open(dep_file, 'r') as f:
            lines = f.readlines()
            read = False
            read_zai = False
            read_name = False
            self.isozai = []
            self.isoname = []
            for line in lines:
                if 'ZAI' in line:
                    read_zai = True
                elif read_zai and ';' in line:
                    read_zai = False
                elif read_zai:
                    self.isozai.append(int(line.strip()))
                if 'NAMES' in line:
                    read_name = True
                elif read_name and ';' in line:
                    read_name = False
                elif read_name:
                    # skip the spaces and first apostrophe
                    self.isoname.append(line.split()[0][1:])
            # gets rid of last, and total values
            self.isozai = self.isozai[:-2]
            self.isoname = self.isoname[:-2]


    def init_db(self):
        """ Initializes the database from the output of the first
            SEPRENT run """
        self.f = h5py.File(self.db_file, 'w')
        self.get_isos()
        self.mat_def_dict = self.get_mat_def()
        self.dep_dict = self.read_dep()
        self.write_run_info()
        self.write_init_mat_def()

        self.number_of_isotopes = len(self.isoname)
        shape = (self.steps, 2)
        maxshape = (None, 2)
        self.keff_eoc_db = self.f.create_dataset('keff_EOC', shape,
                                                 maxshape=maxshape, chunks=True)
        self.keff_boc_db = self.f.create_dataset('keff_BOC', shape,
                                                 maxshape=maxshape, chunks=True)

        shape = (self.steps, self.number_of_isotopes)
        maxshape = (None, self.number_of_isotopes)
        self.driver_before_db = self.f.create_dataset('driver composition before reproc',
                                                      shape, maxshape=maxshape,
                                                      chunks=True)
        self.driver_after_db = self.f.create_dataset('driver composition after reproc',
                                                     shape, maxshape=maxshape,
                                                     chunks=True)
        self.driver_refill_tank_db = self.f.create_dataset('driver refill tank composition',
                                                           shape, maxshape=maxshape,
                                                           chunks=True)

        self.blanket_before_db = self.f.create_dataset('blanket composition before reproc',
                                                       shape, maxshape=maxshape,
                                                       chunks=True)
        self.blanket_after_db = self.f.create_dataset('blanket composition after reproc',
                                                      shape, maxshape=maxshape,
                                                      chunks=True)
        self.blanket_refill_tank_db = self.f.create_dataset('blanket refill tank composition',
                                                            shape, maxshape=maxshape,
                                                            chunks=True)

        self.fissile_tank_db = self.f.create_dataset('fissile tank composition', shape, maxshape=maxshape,
                                                     chunks=True)
        self.waste_tank_db = self.f.create_dataset('waste tank composition',
                                                   shape, maxshape=maxshape,
                                                   chunks=True)
        dt = h5py.special_dtype(vlen=str)
        # have to encode to utf8 for hdf5 string
        self.isolib_db = self.f.create_dataset('iso names',
                                               data=[x.encode('utf8')
                                                     for x in self.isoname],
                                               dtype=dt)
        self.isozai_db = self.f.create_dataset('iso zai', data=self.isozai)
        # the first depleted, non-reprocessed fuel is stored in timestep 1
        # initial composition
        self.dep_dict = self.read_dep()
        self.driver_before_db[0,
                              :] = self.dep_dict[self.driver_mat_name] * self.driver_vol
        try:
            self.blanket_before_db[0,
                                   :] = self.dep_dict[self.blanket_mat_name] * self.blanket_vol
            self.blanket_after_db[0,
                                  :] = self.dep_dict[self.blanket_mat_name] * self.blanket_vol
        except:
            self.blanket_before_db[0, :] = np.zeros(self.number_of_isotopes)
            self.blanket_after_db[0, :] = np.zeros(self.number_of_isotopes)
            print('Blanket not defined: going to be all zeros')

    def write_run_info(self):
        """ Reads from the input file to write to hdf5
            of important SERPENT and Saltproc run parameters
        """
        # read from input file:
        with open(self.input_file, 'r') as f:
            lines = f.readlines()
            for linenum, line in enumerate(lines):
                if (line.split('%')[0]).strip() == 'dep':
                    timestep = (lines[linenum+2].split('%')[0]).strip()
                    if ' ' in timestep:
                        raise ValueError(
                            'Your Input file should only have one depstep')
                if 'set pop' in line and '%' not in line:
                    neutrons = int(line.split()[2])
                    active = int(line.split()[3])
                    inactive = int(line.split()[4])
        # write to db
        self.f.create_dataset('siminfo_timestep', data=timestep)
        self.f.create_dataset('siminfo_pop', data=[neutrons, active, inactive])
        self.f.create_dataset('siminfo_totsteps', data=self.steps)

    def write_init_mat_def(self):
        """ Extracts material density from initial input file
            and records it to the hdf5 file
        """
        # fuel and blanket density
        dens_dict = {}
        for key, value in self.mat_def_dict.items():
            if float(value.split()[2]) < 0:
                cat = 'mass'
                dens_dict[key] = -1.0 * float(value.split()[2])
            else:
                cat = 'atomic'
                dens_dict[key] = float(value.split()[2])
        for key, value in dens_dict.items():
            key = self.get_key_from_mat_name(key)
            self.f.create_dataset('siminfo_%s_%s_density' %(key, cat), data=value)

        init_comp = self.read_dep(boc=True)
        for key, value in init_comp.items():
            key = self.get_key_from_mat_name(key)
            self.f.create_dataset('siminfo_%s_init_comp' %key, data=value)


    def get_key_from_mat_name(self, key):
        """ Returns either `driver' or `blanket' given
            the key from a dictionary

        Parameters:
        -----------
        key: string
            key from dictionary

        Returns:
        --------
        either `driver' or `blanket'
        """
        if key == self.driver_mat_name:
            return 'driver'
        elif key == self.blanket_mat_name:
            return 'blanket'

    def reopen_db(self, restart):
        """ Reopens the previously exisiting database

        Parameters:
        -----------
        restart: bool
            if True, modified current_step and datasets
            if False, simply load the datasets
        """
        self.f = h5py.File(self.db_file, 'r+')
        self.keff_eoc_db = self.f['keff_EOC']
        self.keff_boc_db = self.f['keff_BOC']
        self.driver_before_db = self.f['driver composition before reproc']
        self.driver_refill_tank_db = self.f['driver refill tank composition']
        self.driver_after_db = self.f['driver composition after reproc']

        self.blanket_before_db = self.f['blanket composition before reproc']
        self.blanket_after_db = self.f['blanket composition after reproc']
        self.blanket_refill_tank_db = self.f['blanket refill tank composition']

        self.waste_tank_db = self.f['waste tank composition']
        self.isolib_db = self.f['iso names']
        self.fissile_tank_db = self.f['fissile tank composition']
        self.isozai_db = self.f['iso zai']

        if restart:
            self.restart_sequence()

    def restart_sequence(self):
        #!!! Check this
        # print(list(self.isolib_db))
        self.isoname = [str(x).decode() for x in self.isolib_db]

        # check this too
        # print(list(self.isozai_db))
        self.isozai = self.isozai_db

        self.get_mat_def()
        self.number_of_isotopes = len(self.isoname)

        # get index of first zero in keff

        self.current_step = self.find_prev_run_timestep()

        self.core = {}
        self.core[self.driver_mat_name] = self.driver_after_db[self.current_step -2]
        self.core[self.blanket_mat_name] = self.blanket_after_db[self.current_step -2]
        self.write_mat_file()

    def find_prev_run_timestep(self):
        keff_list = np.array(self.keff_eoc_db)[:,0]
        # find the first zero occurance
        prev_run_timestep = [val==0 for val in keff_list].index(1)
        return prev_run_timestep

    def read_res(self, moment):
        """ Reads SERPENT output .res file

        Parameters:
        -----------
        moment: int
            moment of depletion step (0 for BOC and 1 for EOC)

        Returns:
        --------
        [mean_keff, uncertainty_keff]
        """
        res_filename = os.path.join(self.input_file + "_res.m")
        count = 0
        with open(res_filename, 'r') as f:
            lines = f.readlines()
            for line in lines:
                if 'IMP_KEFF' in line:
                    line = line.split('=')[1]
                    line = line.split('[')[1]
                    line = line.split(']')[0]
                    line = line.split()
                    keff = [float(line[0]), float(line[1])]
                    if count == moment:
                        return keff
                    count += 1

    def read_dep(self, boc=False):
        """ Reads the SERPENT _dep.m file

        Parameters:
        -----------
        moment: int
            moment of depletion step (0 for BOC and 1 for EOC)
        mat_name: string
            name of material to return the composition of
        boc: bool
            if true, gets the boc composition

        Returns:
        --------
        dep_dict: dictionary
            key: material name
            value: np array of mdens
        """
        dep_file = os.path.join('%s_dep.m' % self.input_file)
        with open(dep_file, 'r') as f:
            lines = f.readlines()
            dep_dict = OrderedDict({})
            read = False
            for line in lines:
                if 'MAT'in line and 'MDENS' in line:
                    key = line.split('_')[1]
                    read = True
                    dep_dict[key] = np.zeros(self.isoname)
                elif read and ';' in line:
                    read = False
                elif read:
                    z = line.split(' ')
                    # last burnup stage
                    if boc:
                        mdens = z[0]
                    else:
                        mdens = z[1]
                    # the isotope name is at the end of the line.
                    name = z[-1].replace('\n', '')
                    # find index so that it doesn't change
                    try:
                        where_in_isoname = self.isoname.index(name)
                        dep_dict[key][where_in_isoname] = float(mdens)
                    except ValueError:
                        if name not in ['total', 'data']:
                            print('This isotope is not a valid isotope %s' % name)
        return dep_dict

    def write_mat_file(self):
        """ Writes the input fuel composition input file block

        Parameters:
        -----------

        Returns:
        --------
        null. creates SEPRENT input mat block text file
        """
        ana_keff_boc = self.read_res(0)
        ana_keff_eoc = self.read_res(1)
        matf = open(self.mat_file, 'w')
        matf.write('%% Step number # %i %f +- %f;%f +- %f \n' %
                   (self.current_step, ana_keff_boc[0], ana_keff_boc[1],
                    ana_keff_eoc[0], ana_keff_eoc[1]))
        for key, val in self.core.items():
            if key == '':
                continue
            matf.write(self.mat_def_dict[key].replace('\n', '') + ' fix 09c 900\n')
            for indx, isotope in enumerate(self.isozai):
                if self.check_isozai_metastable(isotope) or not self.check_isotope_in_library(isotope):
                    continue
                isotope = self.add_isozai_temp(isotope, '.09c')
                # filter isotopes not in cross section library
                mass_frac = -1.0 * (val[indx] / sum(val)) * 100
                matf.write('%s\t\t%s\n' % (str(isotope), str(mass_frac)))
        matf.close()

    def check_isozai_metastable(self, isotope):
        """ check if an isotope is metastable by checking its
            last digit
        Returns:
        --------
        bool:
            True if metastable
            False if not
        """
        if str(isotope)[-1] != '0':
            return True
        else:
            return False

    def add_isozai_temp(self, isotope, temp_suffix):
        """ Appends SEREPENT temperature suffix to isozai

        Parameters:
        -----------
        isotope: str
            isotope in zai format
        temp_suffix: str
            temperature suffix for SERPENT (e.g. .09c)

        Returns:
        --------
        str:
            isotope + temp_suffix
        """
        return str(isotope)[:-1] + temp_suffix

    def check_isotope_in_library(self, isotope):
        """ Check if an isotope is in the acelib library
            used for this simulation
        Returns:
        --------
        bool:
            True if  in library
            False if not in library
        """
        if isotope not in self.lib_isos:
            return False
        else:
            return True

    def separate_fuel(self):
        """ separate fissile material from blanket,
            separate waste out of driver
        """
        self.core = self.read_dep()

        self.mdens_to_mass()

        self.core_mass = {}
        for key, val in self.core.items():
            self.core_mass[key] = sum(val)

        # record the depleted composition before reprocessing
        self.driver_before_db[self.current_step,
                              :] = self.core[self.driver_mat_name]
        self.blanket_before_db[self.current_step,
                               :] = self.core[self.blanket_mat_name]

        # waste / fissile tank db initialization
        self.waste_tank_db[self.current_step,
                           :] = self.waste_tank_db[self.current_step-1, :]
        self.fissile_tank_db[self.current_step,
                             :] = self.fissile_tank_db[self.current_step-1, :]
        self.removal()

        self.get_core_space()

        self.inter_material_transfer()

    def get_core_mass(self):
        """ calculates core mass and saves it in dictionary """
        self.core_mass = {}
        for key, val in self.core.items():
            self.core_mass[key] = sum(val)

    def mdens_to_mass(self):
        """ Calculates mass of each material from mdens by multiplying by volume """
        self.core[self.driver_mat_name] = self.core[self.driver_mat_name] * \
            self.driver_vol
        try:
            self.core[self.blanket_mat_name] = self.core[self.blanket_mat_name] * \
                self.blanket_vol
        except:
            self.core[self.blanket_mat_name] = np.zeros(
                self.number_of_isotopes)

    def removal(self):
        """ Removes elements from core from user-defined reprocessing scheme"""
        for group, scheme in self.rep_scheme.items():
            iso_indx = self.find_iso_indx(scheme['element'])
            if scheme['to'] == 'waste':
                self.waste_tank_db[self.current_step, :] += self.remove_iso(iso_indx,
                                                                            scheme['eff'],
                                                                            scheme['from'])
                print('Removing %f kg of %s from %s' %(self.removed_qty, group, scheme['from']))
            else:
                continue

    def get_core_space(self):
        """ Calculates core space after removal and saves it in dictionary"""
        self.core_space = {}
        for mat, val in self.core.items():
            self.core_space[mat] = self.core_mass[mat] - sum(val)

    def inter_material_transfer(self):
        for group, scheme in self.rep_scheme.items():
            if scheme['to'] != 'waste' and scheme['from'] != 'fertile':
                removed = self.remove_iso(
                    iso_indx, scheme['eff'], scheme['from'])
                print('Removing %f kg of %s from %s' %
                    (self.removed_qty, group, scheme['from']))
                if sum(removed) > self.core_space[scheme['to']]:
                    removed_comp = removed / sum(removed)
                    self.core[scheme['to']] += removed_comp * \
                        self.core_space[scheme['to']]
                    print('Moving %f kg of %s from %s to %s' %
                        (self.core_space[scheme['to']], group, scheme['from'], scheme['to']))
                    # remaining amount (surplus)
                    self.fissile_tank_db[self.current_step, :] += removed_comp * \
                        (sum(removed) - self.core_space[scheme['to']])
                    print('Moving %f kg of %s from %s to fissile tank' %
                        (self.core_space[scheme['to']], group, scheme['from']))

    def refuel(self):
        """ After separating out fissile and waste material,
            this function refuels the salt with fissile and fertile
            material
        """
        # refill tank db initialization
        self.driver_refill_tank_db[self.current_step,
                                   :] = self.driver_refill_tank_db[self.current_step-1, :]
        self.blanket_refill_tank_db[self.current_step,
                                    :] = self.blanket_refill_tank_db[self.current_step-1, :]

        for group, scheme in self.rep_scheme.items():
            if scheme['from'] != 'fertile':
                continue
            else:
                qty_to_fill = self.core_mass[scheme['to']
                                             ] - sum(self.core[scheme['to']])
                for indx, frac in enumerate(scheme['comp']):
                    isoid = self.find_iso_indx(scheme['element'][indx])
                    self.refill(isoid, qty_to_fill*frac, scheme['to'])
                    print('Adding in %f kg of %s to %s' % (
                        qty_to_fill * frac, scheme['element'][indx], scheme['to']))

    def reactivity_control(self):
        """ Controls fraction of fissile material
            input into core to control keff into
            a range
        """
        self.eoc_keff = self.read_res(1)
        # how much pu we lost:
        pu = self.find_iso_indx(['Pu'])
        pu_loss = self.driver_before_db[self.current_step,
                                        pu] - self.driver_after_db[self.current_step-1, pu]
        pu_loss = sum(pu_loss)
        pu_avail = sum(self.fissile_tank_db[self.current_step, :])
        print('EOC KEFF is %f +- %f' % (self.eoc_keff[0], self.eoc_keff[1]))

        if self.eoc_keff[0] > 1.05:
            print('KEFF is too high: Not putting any more Pu in driver\n')
            qty = 0
        elif self.eoc_keff[0] <= 1.05 and self.eoc_keff[0] > 1.01:
            print('KEFF is in a good spot: Inserting the amount lost from previous depletion to driver\n')
            qty = min(pu_avail, pu_loss)
        elif self.eoc_keff[0] <= 1.01:
            print('KEFF is low: Inserting 1.5 times Pu than previous step to driver\n')
            qty = min(self.prev_qty * 1.5, pu_avail)
        if qty == pu_avail:
            print('NOT enough Pu available: Inserting maximum amount of Pu available to driver\n')

        self.prev_qty = qty
        return qty

    def record_db(self):
        """ Records the processed fuel composition, Keff values,
            waste tank composition to database
        """
        self.keff_eoc_db[self.current_step - 1, :] = self.read_res(1)
        self.keff_boc_db[self.current_step - 1, :] = self.read_res(0)

        self.driver_after_db[self.current_step,
                             :] = self.core[self.driver_mat_name]
        self.blanket_after_db[self.current_step,
                              :] = self.core[self.blanket_mat_name]

        self.f.close()

    def run_serpent(self):
        """ Runs SERPENT with subprocess with the given parameters"""
        # !why a string not a boolean
        if self.bw:
            args = ('aprun', '-n', str(self.nodes), '-d', str(32),
                    self.exec_path,
                    '-omp', str(32), self.input_file)
        else:
            args = (self.exec_path,
                    '-omp', str(self.cores), self.input_file)
        print('Running Serpent')
        try:
            output = subprocess.check_output(args)
        except subprocess.CalledProcessError as e:
            print(e.output)
            raise ValueError('\nSEPRENT FAILED\n')
        print('Finished Serpent Run')

    def remove_iso(self, target_iso, removal_eff, region):
        """ Removes isotopes with given removal efficiency

        Parameters:
        -----------
        target_iso: array
            array  of indices for isotopes to remove from core
        removal_eff: float
            removal efficiency (max 1)
        region: string
            region to perform action on

        Returns:
        --------
        tank_stream: array
            array of adens of removed material
        """
        self.removed_qty = 0
        tank_stream = np.zeros(self.number_of_isotopes)
        for iso in target_iso:
            tank_stream[iso] = self.core[region][iso] * removal_eff
            self.core[region][iso] = (1 - removal_eff) * self.core[region][iso]
            # print('REMOVING %f GRAMS OF %s FROM %s' %(self.core[region][iso], self.isoname[iso], region))
        self.removed_qty = sum(tank_stream)
        return tank_stream

    def refill(self, refill_iso, delta, region):
        """ Refills isotope with target rate of refuel

        Parameters:
        -----------
        refill_iso: array
            array of indices for isotopes to be refilled
        delta: float
            amount to be refilled
        region: string
            region to perform action on


        Returns:
        --------
        null.
        """
        for iso in refill_iso:
            self.core[region][iso] = self.core[region][iso] + delta
            # print('REFILLING %f GRAMS OF %s TO %s' %(delta, self.isoname[iso], region))
            if region == self.driver_mat_name:
                self.driver_refill_tank_db[self.current_step, iso] -= delta
            elif region == self.blanket_mat_name:
                self.blanket_refill_tank_db[self.current_step, iso] -= delta

    def maintain_const(self, target_isotope, target_qty, region):
        """ Maintains the constant amount of a target isotope

        Parameters:
        -----------
        target_isotope: array
            array of indices for isotopes to be refilled
        target_qty: float
            quantity to be satisfied
        region: string
            region to perform action on

        Returns:
        --------
        null.
        """
        tank_stream = np.zeros(self.number_of_isotopes)
        for iso in target_isotope:
            tank_stream[iso] = self.core[region][iso] - target_qty
            self.core[region][iso] = tartget_qty
        return tank_stream

    def start_sequence(self):
        """ checks restart and preexisting file
            copies initial mat file to predefined mat file name
        """
        if self.restart:
            try:
                self.f = h5py.File(self.db_file, 'r+')
            except:
                raise ValueError('HDF5 File does not exist.\n'
                                 'Are you sure you want to restart?')
            self.reopen_db(True)
            self.steps += self.current_step

        else:
            if os.path.isfile(self.db_file):
                print('File already exists: the file is moved to %s' %
                      self.db_file.replace('.hdf5', '_old.hdf5'))
                os.rename(self.db_file, self.db_file.replace(
                    '.hdf5', '_old.hdf5'))
            print('Copying %s to %s so the initial material file is unchanged..'
                  % (self.init_mat_file, self.mat_file))
            shutil.copy(self.init_mat_file, self.mat_file)

    def main(self):
        """ Core of saltproc: moves forward in timesteps,
            run serpent, process fuel, record to db, and repeats
        """
        self.start_sequence()

        while self.current_step < self.steps:
            print('Cycle number of %i of %i steps' %
                  (self.current_step + 1, self.steps))
            self.run_serpent()
            if self.current_step == 0:
                # intializing db to get all arrays for calculation
                self.init_db()
            else:
                self.reopen_db(False)
            self.current_step += 1
            self.separate_fuel()
            self.refuel()
            self.write_mat_file()
            u235_id = self.find_iso_indx('U235')
            print(self.driver_before_db[self.current_step, u235_id])
            self.record_db()

        print('End of Saltproc.')
