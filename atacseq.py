import os
import subprocess
import re
import time
import pysam
from chunkypipes.components import Software, Parameter, Redirect, Pipe, BasePipeline

"""
TODO It would be cool to input paired-end fastq as /path/to/sample.R*.fastq.gz, but it would
have to be quoted on the command line
"""

READ1 = 0
FIRST_CHAR = 0

MINUS_STRAND_SHIFT = -5
PLUS_STRAND_SHIFT = 4

STERIC_HINDRANCE_CUTOFF = 38


class Pipeline(BasePipeline):
    def description(self):
        return """Pipeline used by the PsychENCODE group at University of Chicago to
        analyze ATACseq samples."""

    def configure(self):
        return {
            'cutadapt': {
                'path': 'Full path to cutadapt executable'
            },
            'bwa': {
                'path': 'Full path to bwa executable',
                'threads': 'Number of threads to use for bwa aln',
                'index-dir': 'Directory of the bwa reference index [Ex. /path/to/bwa/index/genome.fa]'
            },
            'fastqc': {
                'path': 'Full path to FastQC'
            },
            'samtools': {
                'path': 'Full path to samtools'
            },
            'novosort': {
                'path': 'Full path to novosort',
                'threads': 'Number of threads to use for Novosort'
            },
            'picard': {
                'path': 'Full path to Picard [Ex. java -jar /path/to/picard.jar]'
            },
            'bedtools': {
                'path': 'Full path to bedtools >= 2.25.0',
                'blacklist-bed': 'Full path to the BED of blacklisted genomic regions',
                'genome-sizes': 'Full path to a genome sizes file'
            },
            'makeTagDirectory': {
                'path': 'Full path to HOMER makeTagDirectory'
            },
            'findPeaks': {
                'path': 'Full path to HOMER findPeaks'
            },
            'pos2bed': {
                'path': 'Full path to HOMER pos2bed.pl'
            }
        }

    def add_pipeline_args(self, parser):
        parser.add_argument('--reads', required=True, help='read1:read2', action='append')
        parser.add_argument('--output', required=True)
        parser.add_argument('--lib', default=str(time.time()))
        parser.add_argument('--step', default=0)
        parser.add_argument('--forward-adapter', default='ZZZ')
        parser.add_argument('--reverse-adapter', default='ZZZ')
        return parser

    @staticmethod
    def count_gzipped_lines(filepath):
        zcat = subprocess.Popen(['zcat', filepath], stdout=subprocess.PIPE)
        num_lines = subprocess.check_output(['wc', '-l'], stdin=zcat.stdout)
        return num_lines.strip()

    @staticmethod
    def shift_reads(input_bed_filepath, output_bed_filepath, genome_sizes_filepath,
                    log_filepath, minus_strand_shift, plus_strand_shift):
        def to_log(msg, log_filepath):
            with open(log_filepath, 'a') as log_file:
                log_file.write(msg + '\n')

        def within_genome_boundry(chrom, check_pos, genome_sizes_dict):
            if check_pos < 1:
                return False
            if check_pos > genome_sizes_dict[chrom]:
                return False
            return True

        genome_sizes = {}
        with open(genome_sizes_filepath) as genome_sizes_file:
            for line in genome_sizes_file:
                chrom, size = line.strip().split('\t')
                genome_sizes[chrom] = int(size)

        output_bed = open(output_bed_filepath, 'w')
        with open(input_bed_filepath) as input_bed:
            for line in input_bed:
                record = line.strip().split('\t')
                chrom, start, end, strand = record[0], int(record[1]), int(record[2]), record[5]

                if strand == '+':
                    new_start = start + plus_strand_shift
                    new_end = end + plus_strand_shift
                elif strand == '-':
                    new_start = start + minus_strand_shift
                    new_end = end + minus_strand_shift
                else:
                    to_log('Mal-formatted line in file, skipping:', log_filepath)
                    to_log(line.strip(), log_filepath)
                    continue

                if not within_genome_boundry(chrom, new_start, genome_sizes):
                    to_log('Shift start site beyond chromosome boundry:', log_filepath)
                    to_log(line.strip(), log_filepath)
                    continue
                if not within_genome_boundry(chrom, new_end, genome_sizes):
                    to_log('Shift end site beyond chromosome boundry:', log_filepath)
                    to_log(line.strip(), log_filepath)
                    continue

                output_bed.write('\t'.join([chrom, str(new_start), str(new_end)] + record[3:]) + '\n')

        output_bed.flush()
        output_bed.close()

    def run_pipeline(self, pipeline_args, pipeline_config):
        # Instantiate variables from argparse
        read_pairs = pipeline_args['reads']
        output_dir = os.path.abspath(pipeline_args['output'])
        logs_dir = os.path.join(output_dir, 'logs')
        lib_prefix = pipeline_args['lib']
        step = int(pipeline_args['step'])
        forward_adapter = pipeline_args['forward_adapter']
        reverse_adapter = pipeline_args['reverse_adapter']

        # Create output, tmp, and logs directories
        tmp_dir = os.path.join(output_dir, 'tmp')
        subprocess.call(['mkdir', '-p', output_dir, tmp_dir, logs_dir])

        # Keep list of items to delete
        staging_delete = [tmp_dir]
        bwa_bam_outs = []
        qc_data = {
            'total_raw_reads_counts': [],
            'trimmed_reads_counts': [],
            # TODO Find a better way to store FastQC results
            'num_reads_mapped': [],
            'percent_duplicate_reads': '0',
            'num_unique_reads_mapped': [],  # TODO This isn't implemented
            'num_mtDNA_reads_mapped': [],  # TODO This isn't implemented
            'num_reads_mapped_after_filtering': '-1',  # TODO This isn't implemented
            'num_peaks_called': '-1',
            # TODO Get number of peaks in annotation sites
        }

        # Instantiate software instances
        cutadapt = Software('cutadapt', pipeline_config['cutadapt']['path'])
        fastqc = Software('FastQC', pipeline_config['fastqc']['path'])
        bwa_aln = Software('BWA aln', pipeline_config['bwa']['path'] + ' aln')
        bwa_sampe = Software('BWA sampe', pipeline_config['bwa']['path'] + ' sampe')
        samtools_view = Software('samtools view',
                                 pipeline_config['samtools']['path'] + ' view')
        samtools_flagstat = Software('samtools flagstat',
                                     pipeline_config['samtools']['path'] + ' flagstat')
        samtools_index = Software('samtools index',
                                  pipeline_config['samtools']['path'] + ' index')
        novosort = Software('novosort', pipeline_config['novosort']['path'])
        picard_mark_dup = Software('Picard MarkDuplicates',
                                   pipeline_config['picard']['path'] + ' MarkDuplicates')
        picard_insert_metrics = Software('Picard CollectInsertSizeMetrics',
                                         pipeline_config['picard']['path'] + ' CollectInsertSizeMetrics')
        bedtools_bamtobed = Software('bedtools bamtobed',
                            pipeline_config['bedtools']['path'] + ' bamtobed')
        bedtools_sort = Software('bedtools sort', pipeline_config['bedtools']['path'] + ' sort')
        bedtools_merge = Software('bedtools merge', pipeline_config['bedtools']['path'] + ' merge')
        bedtools_intersect = Software('bedtools intersect',
                                      pipeline_config['bedtools']['path'] + ' intersect')
        homer_maketagdir = Software('HOMER makeTagDirectory',
                                    pipeline_config['makeTagDirectory']['path'])
        homer_findpeaks = Software('HOMER findPeaks', pipeline_config['findPeaks']['path'])
        homer_pos2bed = Software('HOMER pos2bed', pipeline_config['pos2bed']['path'])

        if step <= 1:
            for i, read_pair in enumerate(read_pairs):
                read1, read2 = read_pair.split(':')

                # QC: Get raw fastq read counts
                qc_data['total_raw_reads_counts'].append([
                    str(int(self.count_gzipped_lines(read1))/4),
                    str(int(self.count_gzipped_lines(read2))/4)
                ])

                trimmed_read1_filename = os.path.join(output_dir,
                                                      lib_prefix + '_{}_read1.trimmed.fastq.gz'.format(i))
                trimmed_read2_filename = os.path.join(output_dir,
                                                      lib_prefix + '_{}_read2.trimmed.fastq.gz'.format(i))

                cutadapt.run(
                    Parameter('--quality-base=33'),
                    Parameter('--minimum-length=5'),
                    Parameter('-q', '30'),  # Minimum quality score
                    Parameter('--output={}'.format(trimmed_read1_filename)),
                    Parameter('--paired-output={}'.format(trimmed_read2_filename)),
                    Parameter('-a', forward_adapter if forward_adapter else 'ZZZ'),
                    Parameter('-A', reverse_adapter if reverse_adapter else 'ZZZ'),
                    Parameter(read1),
                    Parameter(read2),
                    Redirect(stream=Redirect.STDOUT, dest=os.path.join(logs_dir, 'cutadapt.summary.log'))
                )

                # QC: Get trimmed fastq read counts
                qc_data['trimmed_reads_counts'].append([
                    str(int(self.count_gzipped_lines(trimmed_read1_filename))/4),
                    str(int(self.count_gzipped_lines(trimmed_read2_filename))/4)
                ])

                staging_delete.extend([trimmed_read1_filename, trimmed_read2_filename])
                read_pairs[i] = ':'.join([trimmed_read1_filename, trimmed_read2_filename])

        if step <= 2:
            # Make FastQC directory
            fastqc_output_dir = os.path.join(output_dir, 'fastqc')
            subprocess.call(['mkdir', '-p', fastqc_output_dir])
            for i, read_pair in enumerate(read_pairs):
                for read in read_pair.split(':'):
                    fastqc.run(
                        Parameter('--outdir={}'.format(fastqc_output_dir)),
                        Parameter(read)
                    )

                    bwa_aln.run(
                        Parameter('-t', pipeline_config['bwa']['threads']),
                        Parameter(pipeline_config['bwa']['index-dir']),
                        Parameter(read),
                        Redirect(stream=Redirect.STDOUT, dest='{}.sai'.format(read))
                    )

                    staging_delete.append('{}.sai'.format(read))

        if step <= 3:
            for i, read_pair in enumerate(read_pairs):
                read1, read2 = read_pair.split(':')
                bwa_bam_output = os.path.join(output_dir, '{}.{}.bam'.format(lib_prefix, i))

                bwa_sampe.run(
                    Parameter('-a', '2000'),  # Maximum insert size
                    Parameter('-n', '1'),
                    Parameter(pipeline_config['bwa']['index-dir']),
                    Parameter('{}.sai'.format(read1)),
                    Parameter('{}.sai'.format(read2)),
                    Parameter(read1),
                    Parameter(read2),
                    Redirect(stream=Redirect.STDERR, dest=os.path.join(logs_dir, 'bwa_sampe.log')),
                    Pipe(
                        samtools_view.pipe(
                            Parameter('-hSb'),
                            Parameter('-o', bwa_bam_output),
                            Parameter('-')  # Get input from stdin
                        )
                    )
                )

                bwa_bam_outs.append(bwa_bam_output)

        if step <= 4:
            for i, bwa_bam in enumerate(bwa_bam_outs):
                samtools_flagstat.run(
                    Parameter(bwa_bam),
                    Redirect(stream=Redirect.STDOUT, dest=bwa_bam + '.flagstat')
                )

                # QC: Get number of mapped reads from this BAM
                try:
                    with open(bwa_bam + '.flagstat') as flagstats:
                        flagstats_contents = flagstats.read()
                        target_line = re.search(r'(\d+) \+ \d+ mapped', flagstats_contents)
                        if target_line is not None:
                            qc_data['num_reads_mapped'].append(str(int(target_line.group(1))/2))
                        else:
                            qc_data['num_reads_mapped'].append('0')
                except:
                    qc_data['num_reads_mapped'].append('Could not open flagstats {}'.format(
                        bwa_bam + '.flagstat'
                    ))

            sortmerged_bam = os.path.join(output_dir, '{}.sortmerged.bam'.format(lib_prefix))
            steric_filter_bam = os.path.join(output_dir, '{}.steric.bam'.format(lib_prefix))
            duprm_bam = os.path.join(output_dir, '{}.duprm.bam'.format(lib_prefix))
            unique_bam = os.path.join(output_dir, '{}.unique.bam'.format(lib_prefix))
            unmappedrm_bam = os.path.join(output_dir, '{}.unmappedrm.bam'.format(lib_prefix))
            chrmrm_bam = os.path.join(output_dir, '{}.chrmrm.bam'.format(lib_prefix))

            novosort.run(
                Parameter('--threads', pipeline_config['novosort']['threads']),
                Parameter('--tmpcompression', '6'),
                Parameter('--tmpdir', tmp_dir),
                Parameter(*[bam for bam in bwa_bam_outs]),
                Redirect(stream=Redirect.STDOUT, dest=sortmerged_bam),
                Redirect(stream=Redirect.STDERR, dest=os.path.join(logs_dir, 'novosort.log'))
            )

            # This creates a dependency on PySam
            # Removes reads with template length < 38 due to steric hindrence
            samtools_index.run(Parameter(sortmerged_bam))
            sortmerged_bam_alignmentfile = pysam.AlignmentFile(sortmerged_bam, 'rb')
            steric_filter_bam_alignmentfile = pysam.AlignmentFile(steric_filter_bam, 'wb',
                                                                  template=sortmerged_bam_alignmentfile)
            for read in sortmerged_bam_alignmentfile.fetch():
                if abs(int(read.template_length)) >= STERIC_HINDRANCE_CUTOFF:
                    steric_filter_bam_alignmentfile.write(read)

            sortmerged_bam_alignmentfile.close()
            steric_filter_bam_alignmentfile.close()

            # Mark and remove duplicates
            markduplicates_metrics_filepath = os.path.join(logs_dir,
                                                           'mark_dup.metrics')
            picard_mark_dup.run(
                Parameter('INPUT={}'.format(steric_filter_bam)),
                Parameter('OUTPUT={}'.format(duprm_bam)),
                Parameter('TMP_DIR={}'.format(tmp_dir)),
                Parameter('METRICS_FILE={}'.format(markduplicates_metrics_filepath)),
                Parameter('REMOVE_DUPLICATES=true'),
                Parameter('VALIDATION_STRINGENCY=LENIENT'),
                Redirect(stream=Redirect.BOTH, dest=os.path.join(logs_dir, 'mark_dup.log'))
            )

            # QC: Get percent duplicates
            try:
                with open(markduplicates_metrics_filepath) as markdup_metrics:
                    for line in markdup_metrics:
                        if line[FIRST_CHAR] == '#':
                            continue
                        record = line.strip().split('\t')
                        if len(record) == 9:
                            if re.match(r'\d+', record[7]) is not None:
                                qc_data['percent_duplicate_reads'] = record[7]
            except:
                qc_data['percent_duplicate_reads'] = 'Could not open MarkDuplicates metrics'

            # Filter down to uniquely mapped reads
            samtools_view.run(
                Parameter('-b'),
                Parameter('-F', '256'),
                Parameter('-q', '10'),
                Parameter('-o', unique_bam),
                Parameter(duprm_bam)
            )

            # Remove unmapped reads
            samtools_view.run(
                Parameter('-b'),
                Parameter('-F', '12'),
                Parameter('-o', unmappedrm_bam),
                Parameter(unique_bam)
            )

            # Create BAM index, then remove chrM
            samtools_index.run(
                Parameter(unmappedrm_bam)
            )

            # Remove chrM
            all_chr = [Parameter('chr{}'.format(chromosome)) for chromosome in map(str, range(1, 23)) + ['X', 'Y']]
            samtools_view.run(
                Parameter('-b'),
                Parameter('-o', chrmrm_bam),
                Parameter(unmappedrm_bam),
                *all_chr
            )

            # Stage delete for temporary files
            staging_delete.extend([
                sortmerged_bam,
                sortmerged_bam + '.bai',  # BAM index file
                steric_filter_bam,
                unique_bam,
                duprm_bam,
                unmappedrm_bam,
                unmappedrm_bam + '.bai',  # BAM index file
                chrmrm_bam
            ])

        if step <= 5:
            # Generate filename for final processed BAM and BED
            processed_bam = os.path.join(output_dir, '{}.processed.bam'.format(lib_prefix))
            unshifted_bed = os.path.join(output_dir, '{}.unshifted.bed'.format(lib_prefix))
            processed_bed = os.path.join(output_dir, '{}.processed.bed'.format(lib_prefix))

            # staging_delete.append(unshifted_bed)

            # Generate filename for chrM removed BAM
            chrmrm_bam = os.path.join(output_dir, '{}.chrmrm.bam'.format(lib_prefix))

            # Remove blacklisted genomic regions
            bedtools_intersect.run(
                Parameter('-v'),
                Parameter('-abam', chrmrm_bam),
                Parameter('-b', pipeline_config['bedtools']['blacklist-bed']),
                Parameter('-f', '0.5'),
                Redirect(stream=Redirect.STDOUT, dest=processed_bam)
            )

            # QC: Generate insert size metrics PDF
            picard_insert_metrics.run(
                Parameter('INPUT={}'.format(processed_bam)),
                Parameter('OUTPUT={}'.format(os.path.join(logs_dir, lib_prefix + '.insertsize.metrics'))),
                Parameter('HISTOGRAM_FILE={}'.format(os.path.join(logs_dir, lib_prefix + '.insertsize.pdf')))
            )

            # Generate index for processed BAM
            samtools_index.run(
                Parameter(processed_bam)
            )

            # Convert BAM to BED
            bedtools_bamtobed.run(
                Parameter('-i', processed_bam),
                Redirect(stream=Redirect.STDOUT, dest=unshifted_bed)
            )

            staging_delete.append(unshifted_bed)

            # Shifting + strand by 4 and - strand by -5, according to
            # the ATACseq paper

            # This used to be bedtools shift, but they are fired
            self.shift_reads(
                input_bed_filepath=unshifted_bed,
                output_bed_filepath=processed_bed,
                log_filepath=os.path.join(logs_dir, 'shift_reads.logs'),
                genome_sizes_filepath=pipeline_config['bedtools']['genome-sizes'],
                minus_strand_shift=MINUS_STRAND_SHIFT,
                plus_strand_shift=PLUS_STRAND_SHIFT
            )

        if step <= 6:
            processed_bed = os.path.join(output_dir, '{}.processed.bed'.format(lib_prefix))
            homer_tagdir = os.path.join(output_dir, '{}_tagdir'.format(lib_prefix))
            unsorted_peaks = os.path.join(output_dir, '{}.unsorted.peaks.bed'.format(lib_prefix))
            sorted_peaks = os.path.join(output_dir, '{}.sorted.peaks.bed'.format(lib_prefix))
            merged_peaks = os.path.join(output_dir, '{}.peaks.bed'.format(lib_prefix))

            # Populate HOMER tag directory
            homer_maketagdir.run(
                Parameter(homer_tagdir),
                Parameter('-format', 'bed'),
                Parameter(processed_bed),
                Redirect(stream=Redirect.BOTH, dest=os.path.join(logs_dir, 'maketagdir.log'))
            )

            # Run HOMER peak calling program
            homer_findpeaks.run(
                Parameter(homer_tagdir),
                Parameter('-fragLength', '0'),
                Parameter('-fdr', '0.01'),
                Parameter('-localSize', '50000'),
                Parameter('-o', 'auto'),
                Parameter('-style', 'dnase'),
                Parameter('-size', '150'),
                Parameter('-minDist', '50'),
                Redirect(stream=Redirect.BOTH, dest=os.path.join(logs_dir, 'findpeaks.log'))
            )

            # Convert HOMER peaks file to bed format
            homer_pos2bed.run(
                Parameter(os.path.join(homer_tagdir, 'peaks.txt')),
                Redirect(stream=Redirect.STDOUT, dest=unsorted_peaks),
                Redirect(stream=Redirect.STDERR, dest=os.path.join(logs_dir, 'pos2bed.log'))
            )

            # Sort called peaks bed file
            bedtools_sort.run(
                Parameter('-i', unsorted_peaks),
                Redirect(stream=Redirect.STDOUT, dest=sorted_peaks)
            )

            # Merge peaks to create final peaks file
            bedtools_merge.run(
                Parameter('-i', sorted_peaks),
                Redirect(stream=Redirect.STDOUT, dest=merged_peaks)
            )

            # Stage delete for temporary files
            staging_delete.extend([
                unsorted_peaks,
                sorted_peaks
            ])

        # QC: Output QC data to file
        with open(os.path.join(logs_dir, 'qc_metrics.txt'), 'w') as qc_data_file:
            qc_data_file.write(str(qc_data) + '\n')

        # Delete temporary files
        for delete_file in staging_delete:
            subprocess.call(['rm', '-rf', delete_file])
